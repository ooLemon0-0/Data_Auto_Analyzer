"""Task categories and queries, shared by the sidebar and task browser."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


CATEGORIES = ("镔鑫一期", "镔鑫二期", "芜湖", "瑞丰", "其他项目")
STATUSES = (("all", "全部状态"), ("unfinished", "审核未完成"), ("complete", "审核已完成"),
            ("synced", "已同步轻推"), ("ready", "图片已就绪"), ("active", "正在拉取 / 排队"),
            ("paused", "缓存待补齐"), ("cancelled", "已停止"), ("failed", "拉取失败"))
SORTS = (("date_new", "日期：从新到旧"), ("date_old", "日期：从旧到新"),
         ("name", "点位名称"), ("recent", "最近更新"), ("progress", "审核进度：从高到低"))


def project_category(project):
    if project is None:
        return "其他项目"
    explicit = getattr(project, "category", "")
    if explicit:
        return explicit
    source = project.source
    name = f"{project.id} {project.name}".lower()
    kind = source.get("type", "")
    if kind == "binxin_phase2_history" or kind in {"binxin_76_history", "binxin_77_history"} or project.id.startswith("binxin_cc"):
        return "镔鑫二期"
    if "binxin" in name or "镔鑫" in name:
        return "镔鑫一期"
    if "芜湖" in name or "wuhu" in name or "rongtai" in name:
        return "芜湖"
    if "瑞丰" in name or "ruifeng" in name:
        return "瑞丰"
    return "其他项目"


@dataclass
class TaskQuery:
    search: str = ""
    category: str = ""
    start: str = ""
    end: str = ""
    status: str = "all"
    sort: str = "date_new"

    def to_dict(self):
        return asdict(self)


def review_summaries():
    from app import db
    with db.connect() as conn:
        rows = conn.execute("""SELECT s.project_id, s.business_date, s.target_size, s.uploaded_at, s.updated_at,
            COALESCE(SUM(q.decision IN ('correct','incorrect')),0) AS valid_count
            FROM review_sessions s LEFT JOIN review_queue q ON q.session_id=s.id GROUP BY s.id""").fetchall()
    return {f"{r['project_id']}:{r['business_date']}": {"valid": r["valid_count"], "target": r["target_size"],
        "complete": r["valid_count"] >= r["target_size"], "uploaded": bool(r["uploaded_at"]),
        "updated": r["updated_at"]} for r in rows}


def task_rows(jobs, projects, reviews):
    result = []
    for job in jobs:
        review = reviews.get(job["key"], {})
        try:
            updated = datetime.fromisoformat(review.get("updated", "")).timestamp()
        except (ValueError, TypeError):
            updated = 0
        result.append({**job, "category": project_category(projects.get(job["project_id"])),
            "valid": review.get("valid", 0), "review_target": review.get("target", job["target"]),
            "complete": review.get("complete", False), "uploaded": review.get("uploaded", False),
            "last_activity": max(updated, job.get("updated_at") or 0)})
    return result


def filter_tasks(rows, query):
    filtered = []
    for row in rows:
        day, status = row["business_date"], row["status"]
        haystack = f"{row['name']} {row['project_id']} {row['category']} {day}".lower()
        if any(word.lower() not in haystack for word in query.search.split()):
            continue
        if query.category and row["category"] != query.category:
            continue
        if (query.start and day < query.start) or (query.end and day > query.end):
            continue
        matches = {"all": True, "unfinished": not row["complete"], "complete": row["complete"],
            "synced": row["uploaded"], "ready": status == "ready", "active": status in {"running", "queued", "stopping"},
            "paused": status == "paused", "cancelled": status == "cancelled", "failed": status == "failed"}
        if matches.get(query.status, True):
            filtered.append(row)
    def ordering(row):
        group = CATEGORIES.index(row["category"]) if row["category"] in CATEGORIES else len(CATEGORIES)
        day = datetime.strptime(row["business_date"], "%Y-%m-%d").toordinal()
        value = {"date_new": -day, "date_old": day, "name": row["name"],
            "recent": -row["last_activity"], "progress": -row["valid"] / max(1, row["review_target"])}.get(query.sort, -day)
        return group, row["category"], value, row["name"], row["key"]
    return sorted(filtered, key=ordering)


def review_caption(row):
    return "已同步轻推" if row["uploaded"] else "审核完成" if row["complete"] else f"审核 {row['valid']}/{row['review_target']}"


def cache_caption(row):
    return {"ready": "图片已就绪", "running": "正在拉取", "queued": "排队", "stopping": "正在停止",
            "cancelled": "已停止", "paused": "缓存待补齐", "failed": "拉取失败"}.get(row["status"], "")
