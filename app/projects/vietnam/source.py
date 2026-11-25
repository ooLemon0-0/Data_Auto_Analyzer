from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import md5, sha256
import json
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
import requests

from app.core.cancellation import cancel_pause, check_cancelled
from app.core.http import http_session
from app.core.models import SourceItem
from app.core.progress import report_progress
from app.sources.base import DataSource, NetworkUnavailable, NoSourceRecords
from app.desktop.network import running_processes


BEIJING = timezone(timedelta(hours=8))
NETWORK_MESSAGE = "无法拉取，请先配置好越南的网络环境"
VPN_PROCESSES = {"pangpa.exe", "pangps.exe"}
TEMPORARY_HTTP_ERRORS = {408, 425, 429, 500, 502, 503, 504}


class RetryableResponse(Exception):
    pass


class VietnamHistorySource(DataSource):
    parallel_image_downloads = True
    image_cache_revision = "vietnam_review_v1"
    network_button_label = "检查越南网络"

    def __init__(self, project_config):
        super().__init__(project_config)
        self.source = project_config["source"]
        self.base_url = self._origin(self.source["base_url"])
        self.storage_origin = self._origin(self.source["storage_origin"])
        self.camera_name = self.source["camera_name"]

    @staticmethod
    def _origin(value):
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("越南站点与图片服务地址需要配置为 HTTP(S) 源地址")
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _session():
        session = http_session()
        session.trust_env = False
        return session

    @staticmethod
    def _retry(operation, description, *, attempts=4):
        for attempt in range(attempts):
            check_cancelled()
            try:
                return operation()
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code not in TEMPORARY_HTTP_ERRORS:
                    code = exc.response.status_code if exc.response is not None else "未知"
                    raise RuntimeError(f"{description}失败（HTTP {code}）") from None
            except (requests.RequestException, RetryableResponse):
                pass
            check_cancelled()
            if attempt + 1 < attempts:
                report_progress("network", f"{description}暂时失败，正在重试（{attempt + 2}/{attempts}）")
                cancel_pause(2 ** attempt)
        raise NetworkUnavailable(f"{NETWORK_MESSAGE}。{description}多次失败，可继续拉取已中断的任务。")

    @staticmethod
    def _require_vpn():
        if not VPN_PROCESSES.intersection(running_processes()):
            raise NetworkUnavailable(f"{NETWORK_MESSAGE}。未检测到 GlobalProtect 正在运行。")

    def _ensure_route(self):
        check_cancelled()
        self._require_vpn()
        with self._session() as session:
            def probe():
                with session.get(self.base_url + "/", stream=True, timeout=(3, 6), allow_redirects=False) as response:
                    if not response.ok or response.is_redirect:
                        response.raise_for_status()
                        raise RetryableResponse()

            try:
                self._retry(probe, "访问越南目标网站", attempts=2)
            except (NetworkUnavailable, RuntimeError):
                raise NetworkUnavailable(f"{NETWORK_MESSAGE}。GlobalProtect 已启动，越南目标网站暂不可达。") from None

    def network_status(self):
        try:
            self._ensure_route()
        except NetworkUnavailable as exc:
            return {"available": False, "message": str(exc)}
        return {"available": True, "message": "越南目标网站可访问 · GlobalProtect 已启动"}

    def check_available(self):
        return self.network_status()["available"]

    def _request_json(self, session, method, endpoint, **options):
        def request():
            with session.request(method, self.base_url + endpoint, timeout=(4, 25), allow_redirects=False, **options) as response:
                if response.status_code in {401, 403}:
                    return {"success": False, "code": str(response.status_code)}
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError:
                    raise RetryableResponse() from None
                if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                    raise RetryableResponse()
                return result

        return self._retry(request, f"读取越南接口 {endpoint}")

    def _login(self, session):
        auth = self.source.get("auth", {})
        username = os.environ.get("AUTOANALYZER_VIETNAM_USERNAME") or auth.get("username")
        password = os.environ.get("AUTOANALYZER_VIETNAM_PASSWORD") or auth.get("password")
        if not username or not password:
            raise RuntimeError("越南业务网站账号密码未配置，请检查本机配置。")
        session.headers.pop("Authorization", None)
        session.cookies.clear()
        result = self._request_json(session, "GET", "/api/record/doLogin", params={"account": username, "password": password})
        if not result.get("success"):
            raise RuntimeError("越南业务网站登录失败，请检查网站账号密码。")
        if isinstance(result.get("data"), str) and result["data"]:
            session.headers["Authorization"] = result["data"]

    def _query(self, session, endpoint, **options):
        result = self._request_json(session, "POST", endpoint, **options)
        if str(result.get("code")) == "401":
            self._login(session)
            result = self._request_json(session, "POST", endpoint, **options)
        if not result.get("success") or not isinstance(result.get("data"), dict):
            raise RuntimeError(f"越南网站未返回有效数据（{endpoint}）")
        return result["data"]

    def _camera(self, session):
        payload = self._query(session, "/api/v1/camera/condition-query",
                              params={"currPage": 1, "pageSize": 100, "sort": "id,descending"})
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("越南相机列表格式错误")
        matches = [row for row in rows if row.get("cameraName") == self.camera_name and row.get("ocrCamera") is True]
        if len(matches) != 1 or not matches[0].get("id") or not matches[0].get("businessGroupId"):
            raise RuntimeError(f"越南网站未找到唯一的 OCR 相机（{self.camera_name}）")
        return matches[0]

    def _image_path(self, url):
        parsed = urlsplit(url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.storage_origin or parsed.query or parsed.fragment
                or not re.fullmatch(r"/ocr-identification/\d{4}-\d{2}-\d{2}/[\w-]+/[\w.-]+\.(?:jpe?g|png)", parsed.path, re.ASCII | re.I)):
            raise ValueError("越南历史记录的图片地址不属于已配置的图片服务")
        return parsed.path

    def _item(self, record, camera, low, high):
        original = str(record.get("identifyFullUrl") or "")
        detail = str(record.get("identifyPictureUrl") or "")
        urls = [url for url in (original, detail) if url]
        identifiers = {Path(self._image_path(url)).name.split("_", 1)[0] for url in urls}
        explicit = str(record.get("cameraId") or "")
        if explicit:
            identifiers.add(explicit)
        if len(identifiers) > 1:
            raise RuntimeError("越南记录的原图和裁图相机标识不一致")
        if not urls or identifiers != {str(camera["id"])}:
            return None

        stamp = int(record["accTime"])
        if not low <= stamp <= high:
            raise RuntimeError("越南接口返回了超出查询时间范围的记录")
        timestamp = datetime.fromtimestamp(stamp / 1000, BEIJING).strftime("%Y/%m/%d %H:%M:%S")
        url = original or detail
        return SourceItem(
            source_key=f"vietnam_{camera['id']}_{record['id']}",
            recognition_text=str(record.get("identifyBilletNumber") or ""),
            image_url=url,
            metadata={"timestamp": timestamp, "camera_id": str(camera["id"]), "camera_name": self.camera_name,
                      "record_id": record["id"], "image_name": Path(self._image_path(url)).name,
                      "original_image_url": original, "detail_image_url": detail,
                      "review_box": record.get("identifyCoordinate") if original else None,
                      "review_image_revision": self.image_cache_revision,
                      "recognition_status": record.get("identifyStatusEnum") or ""},
        )

    def fetch_day(self, business_date: date):
        report_progress("network", "正在检查越南网络")
        self._ensure_route()
        start = datetime.combine(business_date, time.min, BEIJING)
        low = int(start.timestamp() * 1000)
        high = min(int((start + timedelta(days=1)).timestamp() * 1000) - 1, int(datetime.now(BEIJING).timestamp() * 1000))
        page_size = 80
        seen, items = set(), []
        with self._session() as session:
            session.headers.update({"Accept": "application/json", "X-Server-Origin": "http://127.0.0.1:18899"})
            self._login(session)
            camera = self._camera(session)
            query = {"lowerTime": low, "upperTime": high, "businessGroupId": camera["businessGroupId"]}
            expected = None
            for page in range(1, 201):
                payload = self._query(session, "/api/v1/history/condition-query", json=query,
                                      params={"currPage": page, "pageSize": page_size, "sort": "accTime,descending"})
                count, records = payload.get("rowCount"), payload.get("data")
                if not isinstance(records, list) or type(count) is not int or count < 0:
                    raise RuntimeError("越南历史接口分页格式错误")
                if expected is None:
                    expected = count
                if count != expected or len(records) != min(page_size, expected - len(seen)):
                    raise RuntimeError("越南历史接口返回的分页不完整，请重新拉取")
                for record in records:
                    key = str(record.get("id") or "")
                    if not key or key in seen:
                        raise RuntimeError("越南历史接口返回重复记录或缺少记录 ID")
                    seen.add(key)
                    item = self._item(record, camera, low, high)
                    if item:
                        items.append(item)
                report_progress("records", f"越南历史记录 {len(seen)} / {expected}", len(seen), expected)
                if len(seen) == expected:
                    break
            else:
                raise RuntimeError("越南历史记录超过单次分页上限")
        if not items:
            raise NoSourceRecords(f"{business_date.isoformat()} 越南点位没有可审核记录")
        return items

    def materialize_image(self, image_url, destination):
        self._require_vpn()
        path = self._image_path(image_url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        def download():
            temporary = None
            try:
                with self._session() as session, session.get(
                    self.base_url + "/oss" + path + "?origin=" + self.storage_origin,
                    headers={"Accept-Encoding": "identity"}, timeout=(4, 22), stream=True, allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    if response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                        raise RetryableResponse()
                    digest = md5(usedforsecurity=False)
                    with NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
                        temporary = Path(output.name)
                        for chunk in response.iter_content(65536):
                            check_cancelled()
                            output.write(chunk)
                            digest.update(chunk)
                        size = output.tell()
                    length = response.headers.get("Content-Length")
                    etag = response.headers.get("ETag", "").strip('"')
                    if not size or (length is not None and str(size) != length):
                        raise RetryableResponse()
                    if re.fullmatch(r"[0-9a-fA-F]{32}", etag) and digest.hexdigest() != etag.lower():
                        raise RetryableResponse()
                try:
                    with Image.open(temporary) as image:
                        image.load()
                except (OSError, ValueError):
                    raise RetryableResponse() from None
                check_cancelled()
                temporary.replace(destination)
                return destination
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        return self._retry(download, "下载并校验越南图片")

    def materialize_review_image(self, image_url, destination, metadata, *, original=None, label=""):
        if not metadata.get("original_image_url"):
            metadata = {**metadata, "review_box": None}
        raw = destination.parent / "originals" / (sha256(image_url.encode()).hexdigest() + Path(self._image_path(image_url)).suffix.lower())
        if not raw.is_file():
            self.materialize_image(image_url, raw)
        with Image.open(raw) as opened:
            image = opened.convert("RGB")
        with image:
            box = metadata.get("review_box")
            if isinstance(box, str):
                try:
                    box = json.loads(box)
                except ValueError:
                    box = None
            if isinstance(box, (list, tuple)) and len(box) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in box):
                clamped = [max(0, min(v, image.size[i % 2] - 1)) for i, v in enumerate(box)]
                box = clamped if clamped[0] < clamped[2] and clamped[1] < clamped[3] else None
            else:
                box = None
            if box:
                ImageDraw.Draw(image).rectangle(box, outline="#2CB6B3", width=max(2, image.width // 500))
            temporary = None
            try:
                with NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
                    temporary = Path(output.name)
                    image.save(output, format="PNG", compress_level=3)
                check_cancelled()
                temporary.replace(destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return destination, {"review_image_kind": "boxed" if box else "original" if metadata.get("original_image_url") else "detail",
                             "review_box": box, "original_cache_path": str(raw.resolve())}
