"""统一时间处理：所有对外的展示时间一律为「服务器本机本地时间」。

PostgreSQL 的 timestamptz 经 asyncpg 读出后是 UTC aware datetime，
直接 str() 会比北京时间慢 8 小时。这里统一转换成本地时区后再格式化，
并在 /api/meta 暴露服务器时间供前端时钟校准。
"""
from datetime import datetime, timedelta, timezone

try:  # Windows 上可能缺少 tzdata，回落到固定 +08:00
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
    LOCAL_TZ_NAME = "Asia/Shanghai (UTC+08:00)"
except Exception:  # pragma: no cover
    LOCAL_TZ = timezone(timedelta(hours=8))
    LOCAL_TZ_NAME = "UTC+08:00"

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def now_local() -> datetime:
    """当前本地时间（aware datetime，写入 DB 会自动换算为 UTC 存储）。"""
    return datetime.now(LOCAL_TZ)


def fmt_dt(value) -> str:
    """把 DB 时间 / datetime / 已格式化字符串统一转成本地时间字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        # 无时区信息的旧数据按 UTC 解释
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ).strftime(TIME_FMT)


def fmt_iso(value: datetime | None = None) -> str:
    """带本地时区偏移的 ISO 时间，用于报表。"""
    return (value or now_local()).astimezone(LOCAL_TZ).isoformat(timespec="seconds")


def server_time_info() -> dict:
    n = now_local()
    return {
        "server_now": n.strftime(TIME_FMT),
        "server_now_iso": n.isoformat(timespec="seconds"),
        "timezone": LOCAL_TZ_NAME,
        "utc_offset": n.strftime("%z"),
    }
