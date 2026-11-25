"""Sample source originals across an inclusive date range without creating review records."""
from datetime import datetime, timedelta
import logging
from pathlib import Path
import random
from tempfile import TemporaryDirectory, mkdtemp

from app.core.cancellation import check_cancelled
from app.core.progress import report_progress
from app.core.registry import build_source
from app.desktop.image_export import original_image_suffix
from app.sources.base import NetworkUnavailable, NoSourceRecords


logger = logging.getLogger(__name__)


def collect_originals(project, start, end, quantity, directory):
    if end < start or quantity < 1:
        raise ValueError("请选择有效的采集日期和数量")

    source = build_source(project)
    originals = {}
    days = (end - start).days + 1
    for offset in range(days):
        day = start + timedelta(days=offset)
        report_progress("records", f"正在读取 {day} 的记录", offset, days)
        try:
            records = source.fetch_day(day)
        except NoSourceRecords:
            continue
        for record in records:
            url = (record.metadata.get("original_image_url") or "").strip()
            if url:
                originals.setdefault(url, day)

    if not originals:
        return {"directory": None, "saved": 0, "requested": quantity, "available": 0, "failed": 0, "reason": ""}
    check_cancelled()
    parent = Path(directory)
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(mkdtemp(prefix=f"原图采集_{datetime.now():%Y%m%d_%H%M%S}_", dir=parent))
    candidates = list(originals.items())
    random.shuffle(candidates)
    target = min(quantity, len(candidates))
    saved = failed = 0
    reason = ""
    with TemporaryDirectory(dir=folder, prefix=".download-") as temporary:
        for url, day in candidates:
            report_progress("images", f"已保存 {saved:,} / {target:,} 张原图", saved, target)
            destination = Path(temporary) / "image"
            try:
                original = Path(source.materialize_image(url, destination))
                suffix = original_image_suffix(original, decode=True)
            except NetworkUnavailable as error:
                check_cancelled()
                failed += 1
                reason = str(error)
                break
            except (RuntimeError, ValueError, OSError) as error:
                check_cancelled()
                destination.unlink(missing_ok=True)
                failed += 1
                logger.warning("Original image collection failed for %s %s: %s", project["id"], day, type(error).__name__)
                continue
            check_cancelled()
            original.replace(folder / f"{saved + 1:05d}_{day}{suffix}")
            saved += 1
            if saved == target:
                break

    report_progress("ready", f"已保存 {saved:,} 张原图", saved, target)
    return {"directory": folder, "saved": saved, "requested": quantity, "available": len(candidates),
            "failed": failed, "reason": reason}
