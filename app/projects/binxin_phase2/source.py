from __future__ import annotations

import os
import hashlib
import subprocess
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from threading import RLock
from urllib.parse import unquote, urlparse

import requests

from app.core.config import settings
from app.core.models import SourceItem
from app.core.progress import report_progress, shared_collection_cache
from app.core.http import http_get, http_session
from app.core.cancellation import check_cancelled, cancel_pause, cancellable_lock
from app.sources.base import DataSource
from app.projects.binxin_phase2.review_image import REVISION, parse_box, render_review


CHINA = timezone(timedelta(hours=8))


class BinxinPhase2HistorySource(DataSource):
    """Authenticated OCR API collection, isolated by business group and camera."""

    parallel_image_downloads = True
    image_cache_revision = REVISION

    _locks_guard = RLock()
    _host_locks: dict[str, RLock] = {}
    _image_locks: dict[str, RLock] = {}

    def __init__(self, project_config: dict):
        super().__init__(project_config)
        self.source = project_config["source"]
        self.base_url = str(self.source.get("base_url", "")).rstrip("/")
        self.point_name = str(self.source.get("point_name", "")).strip()
        if not self.base_url or not self.point_name:
            raise ValueError("镔鑫二期点位必须配置 source.base_url 和 source.point_name")

    def _probe(self) -> bool:
        try:
            return http_get(self.base_url + "/", timeout=3).status_code < 500
        except requests.RequestException:
            return False

    def _ensure_route(self) -> None:
        check_cancelled()
        if self._probe():
            return
        check_cancelled()
        ec = self.source.get("easyconnect", {})
        command = ec.get("launch_command") or []
        if ec.get("enabled", True) and not command and os.name == "nt":
            for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
                if not root:
                    continue
                candidate = Path(root) / "Sangfor/SSL/SangforCSClient/SangforCSClient.exe"
                if candidate.exists():
                    command = [str(candidate)]
                    break
        if ec.get("enabled", True) and command:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        timeout = float(ec.get("connect_timeout_seconds", 90)) if ec.get("enabled", True) else 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._probe():
                return
            cancel_pause(2)
        raise RuntimeError(f"镔鑫内网不可达：{self.base_url}；请检查 VPN 连接")

    def check_available(self) -> bool:
        try:
            self._ensure_route()
            return True
        except RuntimeError:
            return False

    def _credentials(self) -> list[tuple[str, str]]:
        auth = self.source.get("auth", {})
        reference = auth.get("credentials_from_project")
        inherited = settings.project(reference).source.get("auth", {}) if reference else {}
        candidates = [{
            "username": auth.get("username") or inherited.get("username"),
            "password": auth.get("password") or inherited.get("password"),
        }, *auth.get("candidates", [])]
        pairs = []
        for candidate in candidates:
            pair = (str(candidate.get("username") or "").strip(), str(candidate.get("password") or ""))
            if all(pair) and pair not in pairs:
                pairs.append(pair)
        if not pairs:
            raise RuntimeError("镔鑫 OCR 网站账号密码未配置")
        return pairs

    def _session(self) -> requests.Session:
        session = http_session()
        session.headers.update({
            "accept": "application/json",
            "referer": self.base_url + "/",
            "x-server-origin": self.source.get("server_origin", "http://127.0.0.1:18899"),
        })
        try:
            for username, password in self._credentials():
                try:
                    response = session.get(
                        self.base_url + "/api/record/doLogin",
                        params={"account": username, "password": password},
                        timeout=20,
                    )
                    response.raise_for_status()
                    envelope = response.json()
                except (requests.RequestException, ValueError) as exc:
                    raise RuntimeError(f"镔鑫 OCR 登录接口不可用：{self.base_url}") from exc
                if isinstance(envelope, dict) and envelope.get("success") is True:
                    return session
            raise RuntimeError(f"镔鑫 OCR 登录失败：{self.base_url}；请检查账号密码")
        except Exception:
            session.close()
            raise

    def _query(self, session: requests.Session, endpoint: str, *, body=None, params=None):
        try:
            response = session.post(
                self.base_url + endpoint,
                params=params,
                json=body,
                timeout=float(self.source.get("api", {}).get("request_timeout_seconds", 60)),
            )
            response.raise_for_status()
            envelope = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"镔鑫接口请求失败：{endpoint}") from exc
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            message = str(envelope.get("message") or "")[:160] if isinstance(envelope, dict) else ""
            raise RuntimeError(f"镔鑫接口未返回成功结果：{endpoint}；{message}")
        return envelope.get("data")

    def _resolve_camera(self, session: requests.Session) -> dict:
        payload = self._query(
            session, "/api/v1/camera/condition-query",
            params={"currPage": 1, "pageSize": 9999, "sort": "id,descending"},
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("镔鑫相机列表格式错误")
        matches = [row for row in rows if row.get("cameraName") == self.point_name]
        if len(matches) != 1:
            raise RuntimeError(f"点位名称未唯一匹配现场相机：{self.point_name}")
        camera = matches[0]
        if not camera.get("id") or not camera.get("businessGroupId"):
            raise RuntimeError(f"点位缺少相机或业务分组标识：{self.point_name}")
        if camera.get("ocrCamera") is False:
            raise RuntimeError(f"该站点中的相机未配置为 OCR 数据源：{self.point_name}")
        return camera

    @staticmethod
    def _day_bounds_ms(business_date: date) -> tuple[int, int]:
        start = datetime.combine(business_date, datetime_time.min, tzinfo=CHINA)
        return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000) - 1

    def _record_to_item(self, record: dict, camera: dict, business_date: date) -> SourceItem | None:
        try:
            acc_time = int(record["accTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("历史记录缺少有效采集时间") from exc
        lower, upper = self._day_bounds_ms(business_date)
        if not lower <= acc_time <= upper:
            raise RuntimeError("历史接口返回了指定日期之外的记录")
        camera_id = str(record.get("cameraId") or "")
        if not camera_id:
            raise RuntimeError("历史记录缺少相机标识，无法安全划分点位")
        if camera_id != str(camera["id"]):
            return None
        image_url = str(record.get("identifyFullUrl") or record.get("identifyPictureUrl") or "").strip()
        record_id = record.get("id")
        if not image_url or record_id is None:
            raise RuntimeError("历史记录缺少唯一标识或图片地址")
        recognition = str(record.get("identifyBilletNumber") or "").strip()
        original_url = str(record.get("identifyFullUrl") or "").strip()
        box = parse_box(record.get("identifyCoordinate")) if original_url else None
        timestamp = datetime.fromtimestamp(acc_time / 1000, CHINA).strftime("%Y/%m/%d %H:%M:%S")
        return SourceItem(
            source_key=f"{self.source['station']}_{camera_id}_{record_id}",
            recognition_text=recognition,
            image_url=image_url,
            metadata={
                "timestamp": timestamp,
                "station": str(self.source["station"]),
                "point_name": self.point_name,
                "camera_id": camera_id,
                "business_group_id": camera["businessGroupId"],
                "record_id": record_id,
                "image_name": Path(unquote(urlparse(image_url).path)).name,
                "image_url": image_url,
                "original_image_url": original_url,
                "detail_image_url": record.get("identifyPictureUrl") or "",
                "identify_coordinate": record.get("identifyCoordinate"),
                "review_box": box,
                "review_image_revision": REVISION,
                "review_image_kind": "boxed" if box else "original" if original_url else "detail",
                "recognition_status": record.get("identifyStatusEnum") or "",
                "recognition_method": record.get("identifyMethodEnum") or "",
                "manual_review_value": record.get("artificialCheckNumber") or "",
            },
        )

    def _fetch_camera_day(self, session, camera: dict, business_date: date) -> list[SourceItem]:
        cache = shared_collection_cache()
        cache_key = ("binxin-history", self.base_url, str(camera["businessGroupId"]), business_date.isoformat())
        if cache is not None and cache_key in cache:
            records = cache[cache_key]
            items = [item for record in records if (item := self._record_to_item(record, camera, business_date)) is not None]
            report_progress("records", f"复用本批次 {len(records):,} 条历史记录 · 匹配点位 {len(items):,} 条", len(records), len(records))
            return items
        lower, upper = self._day_bounds_ms(business_date)
        now_ms = int(time.time() * 1000)
        if lower <= now_ms <= upper:
            upper = now_ms
        body = {"lowerTime": lower, "upperTime": upper, "businessGroupId": camera["businessGroupId"]}
        page_size = int(self.source.get("api", {}).get("page_size", 500))
        max_pages = int(self.source.get("api", {}).get("max_pages", 200))
        if page_size < 1 or max_pages < 1:
            raise ValueError("API 每页记录数和最大页数必须大于 0")
        expected, total_pages, page_no, raw_count = None, 1, 1, 0
        seen, items, records = set(), [], []
        while page_no <= total_pages:
            check_cancelled()
            payload = self._query(
                session, "/api/v1/history/condition-query", body=body,
                params={"currPage": page_no, "pageSize": page_size, "sort": "accTime,descending"},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise RuntimeError("历史接口分页格式错误")
            count = payload.get("rowCount")
            if not isinstance(count, int) or count < 0:
                raise RuntimeError("历史接口缺少有效记录总数")
            if expected is None:
                expected = count
                total_pages = max(1, (count + page_size - 1) // page_size)
                if total_pages > max_pages:
                    raise RuntimeError(f"历史数据超过最大页数 {max_pages}")
            elif count != expected:
                raise RuntimeError("历史数据总数在分页过程中发生变化，请重新拉取")
            for record in payload["data"]:
                if not isinstance(record, dict) or record.get("id") is None:
                    raise RuntimeError("历史记录格式错误")
                key = str(record["id"])
                if key in seen:
                    raise RuntimeError("历史分页返回重复记录")
                seen.add(key)
                records.append(record)
                raw_count += 1
                item = self._record_to_item(record, camera, business_date)
                if item is not None:
                    items.append(item)
            report_progress("records", f"已读取 {raw_count:,} / {expected:,} 条记录 · 匹配点位 {len(items):,} 条", raw_count, expected)
            page_no += 1
        if raw_count != expected:
            raise RuntimeError(f"历史数据不完整：接口报告 {expected} 条，实际获取 {raw_count} 条")
        if cache is not None:
            cache[cache_key] = records
        return items

    def fetch_day(self, business_date: date) -> list[SourceItem]:
        report_progress("network", "正在检查企业内网连接")
        self._ensure_route()
        # Points on one edge box share its database and login service. Serialize
        # their collection while allowing independent boxes to run concurrently.
        with self._locks_guard:
            lock = self._host_locks.setdefault(self.base_url, RLock())
        with cancellable_lock(lock):
            report_progress("records", "正在登录并匹配现场点位")
            with self._session() as session:
                camera = self._resolve_camera(session)
                items = self._fetch_camera_day(session, camera, business_date)
        if not items:
            raise RuntimeError(f"{business_date.isoformat()} 点位“{self.point_name}”没有可审核记录")
        return items

    def materialize_image(self, image_url: str, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".part")
        try:
            response = http_get(image_url, timeout=30)
            response.raise_for_status()
            data = response.content
            valid = data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM"))
            valid = valid or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
            if not valid:
                raise RuntimeError("图片服务返回了非图片内容")
            check_cancelled()
            tmp.write_bytes(data)
            check_cancelled()
            tmp.replace(destination)
            return destination
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"点位图片缓存失败：{self.point_name}") from exc

    def materialize_review_image(self, image_url, destination, metadata, *, original=None, label=""):
        suffix = Path(urlparse(image_url).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            suffix = ".jpg"
        # Several billet detections can share one frame, but their boxes differ.
        # Share only the raw frame; render a separate review image for each item.
        raw = destination.parent / "originals" / (hashlib.sha256(image_url.encode()).hexdigest() + suffix)
        with self._locks_guard:
            lock = self._image_locks.setdefault(str(raw.resolve()), RLock())
        with cancellable_lock(lock):
            if not raw.exists():
                old = Path(original) if original else None
                if old is not None and old.is_file() and ".phase2_review_" not in old.name:
                    # Existing caches already contain the full frame. Reuse it
                    # without downloading it again or overwriting audit evidence.
                    raw = old
                else:
                    self.materialize_image(image_url, raw)
        return render_review(raw, destination, metadata, label)
