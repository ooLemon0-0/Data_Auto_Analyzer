"""First-phase Binxin API transport. No browser, DOM, or Chrome installation."""
from datetime import date, datetime, time as day_time, timedelta, timezone
import base64
import hashlib
from pathlib import Path
import time
from urllib.parse import unquote, unquote_to_bytes, urlsplit

import requests

from app.core.models import SourceItem
from app.core.progress import report_progress
from app.core.http import http_get, http_session
from app.core.cancellation import check_cancelled


CHINA = timezone(timedelta(hours=8))


def day_bounds(business_date: date):
    start = datetime.combine(business_date, day_time.min, tzinfo=CHINA)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000) - 1


def record_to_item(source: dict, record: dict, business_date: date, page: int, row: int):
    lower, upper = day_bounds(business_date)
    try:
        captured = int(record["accTime"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("历史接口记录缺少有效采集时间") from exc
    if not lower <= captured <= upper:
        raise RuntimeError("历史接口返回了目标日期之外的记录")
    image = str(record.get("identifyPictureUrl") or record.get("identifyFullUrl") or "").strip()
    if not image:
        raise RuntimeError("历史接口记录缺少识别图片")
    timestamp = datetime.fromtimestamp(captured / 1000, CHINA).strftime("%Y/%m/%d %H:%M:%S")
    recognition = str(record.get("identifyBilletNumber") or "").strip()
    basename = Path(unquote(urlsplit(image).path)).name
    digest = hashlib.sha1(f"{timestamp}|{basename}|{recognition}|{image}".encode()).hexdigest()[:16]
    return SourceItem(
        source_key=f"{timestamp.replace('/', '').replace(':', '').replace(' ', '_')}_{digest}",
        recognition_text=recognition, image_url=image,
        metadata={"timestamp": timestamp, "station": source.get("station", "73"),
                  "recognition_type": record.get("identifyTypeEnum") or "", "recognition": recognition,
                  "true_value": record.get("identifyBilletNumberReal") or "",
                  "manual_review_value": record.get("artificialCheckNumber") or "",
                  "unrecognized_reason": record.get("reasonNotIdentify") or "",
                  "recognition_status": record.get("identifyStatusEnum") or "",
                  "image_url": image, "image_name": basename,
                  "original_image_url": record.get("identifyFullUrl") or "",
                  "record_id": record.get("id"), "source_page": page, "source_row": row},
    )


def authenticated_session(source):
    parts = urlsplit(source.source["history_url"])
    base = f"{parts.scheme}://{parts.netloc}"
    session = http_session()
    session.headers.update({"accept": "application/json", "referer": base + "/"})
    try:
        response = session.post(base + "/api/account/login", json={
            "username": source._credential("username"), "password": source._credential("password")}, timeout=20)
        response.raise_for_status()
        token = response.headers.get("token")
        if not token:
            raise RuntimeError("网站登录未成功，请联系维护人员检查内置账号")
        session.headers["Authorization"] = token
        return session, base
    except Exception:
        session.close()
        raise RuntimeError("无法登录数据网站，请检查内网连接或联系维护人员") from None


def fetch_day(source, business_date: date):
    report_progress("network", "正在检查企业内网连接")
    source._ensure_easyconnect()
    report_progress("records", "正在登录数据接口")
    session, base = authenticated_session(source)
    with session:
        lower, upper = day_bounds(business_date)
        now = int(time.time() * 1000)
        if lower <= now <= upper:
            upper = now
        cfg = source.source.get("api", {})
        page_size = int(cfg.get("page_size", 500))
        max_pages = int(source.source.get("pagination", {}).get("max_pages", 200))
        if page_size < 1 or max_pages < 1:
            raise ValueError("分页配置必须大于零")
        items, seen, expected, pages, page = [], set(), None, 1, 1
        while page <= pages:
            check_cancelled()
            response = session.post(base + "/api/v1/history/condition-query",
                params={"currPage": page, "pageSize": page_size, "sort": "accTime,descending"},
                json={"lowerTime": lower, "upperTime": upper},
                timeout=float(cfg.get("request_timeout_ms", 60000)) / 1000)
            response.raise_for_status()
            envelope = response.json()
            payload = envelope.get("data") if isinstance(envelope, dict) else None
            if not isinstance(envelope, dict) or envelope.get("success") is False or not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise RuntimeError("历史接口返回格式错误，请重新拉取")
            count = payload.get("rowCount")
            if not isinstance(count, int) or count < 0:
                raise RuntimeError("历史接口缺少有效记录总数")
            if expected is None:
                expected = count
                pages = max(1, (count + page_size - 1) // page_size)
                if pages > max_pages:
                    raise RuntimeError("当天数据超过最大分页范围")
            elif count != expected:
                raise RuntimeError("历史数据总数在分页期间发生变化，请重新拉取")
            for row, record in enumerate(payload["data"], 1):
                if not isinstance(record, dict) or record.get("id") is None:
                    raise RuntimeError("历史记录缺少唯一标识")
                item = record_to_item(source.source, record, business_date, page, row)
                key = str(record["id"])
                if key in seen:
                    raise RuntimeError("历史分页返回重复记录，请重新拉取")
                seen.add(key)
                item.metadata.update(filter_start=business_date.isoformat() + " 00:00:00",
                                     filter_end=business_date.isoformat() + " 23:59:59", data_export_archive="")
                items.append(item)
            report_progress("records", f"已读取 {len(items):,} / {expected:,} 条记录", len(items), expected)
            page += 1
        if len(items) != expected:
            raise RuntimeError(f"历史数据拉取不完整：应有 {expected} 条，实际 {len(items)} 条")
        if not items:
            raise RuntimeError(f"{business_date.isoformat()} 没有可审核记录，请选择其他日期")
        return items


def materialize_image(source, image_url: str, destination: Path):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    if image_url.startswith("data:"):
        header, encoded = image_url.split(",", 1)
        data = base64.b64decode(encoded) if ";base64" in header else unquote_to_bytes(encoded)
        return source._save_image_bytes(data, header, destination, tmp)
    response = http_get(image_url, headers={"Referer": source.source["history_url"]}, timeout=30)
    if response.status_code in (401, 403):
        session, _base = authenticated_session(source)
        with session:
            response = session.get(image_url, timeout=30)
    response.raise_for_status()
    return source._save_image_bytes(response.content, response.headers.get("content-type", ""), destination, tmp)
