"""通用工具：UTC 时间、日期边界、数值格式化与舍入。"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP


class HourFormatError(ValueError):
    """小时时间戳格式不合法。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_hour(raw: str) -> tuple[str, bool]:
    """把 ISO 8601 时间转换为规范的小时字符串。

    返回 (规范字符串, 是否使用了无时区输入)。无时区输入按 UTC 处理。
    要求时间精确到小时（分、秒、微秒均为 0）。
    """

    value = (raw or "").strip()
    if not value:
        raise HourFormatError("时间为空")
    if "T" not in value and " " not in value:
        raise HourFormatError("时间必须包含小时，例如 2026-11-20T00:00:00Z")

    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HourFormatError("时间格式无法解析，请使用 ISO 8601（如 2026-11-20T08:00:00Z）") from exc

    naive = parsed.tzinfo is None
    if naive:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)

    if parsed.minute or parsed.second or parsed.microsecond:
        raise HourFormatError("小时数据必须对齐到整点，分/秒必须为 0")
    if not 1970 <= parsed.year <= 2100:
        raise HourFormatError("年份超出合理范围（1970-2100）")

    return parsed.strftime("%Y-%m-%dT%H:00:00Z"), naive


def parse_report_date(raw: str) -> str:
    """校验 YYYY-MM-DD 日期并返回规范字符串。"""

    value = (raw or "").strip()
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期格式应为 YYYY-MM-DD，例如 2026-11-20") from exc
    if parsed.isoformat() != value:
        raise ValueError("日期格式应为 YYYY-MM-DD，例如 2026-11-20")
    return parsed.isoformat()


def day_bounds(day: str) -> tuple[str, str]:
    """返回 UTC 日的 [start, end) 规范时间字符串。"""

    parsed = date.fromisoformat(parse_report_date(day))
    start = f"{parsed.isoformat()}T00:00:00Z"
    end = f"{(parsed + timedelta(days=1)).isoformat()}T00:00:00Z"
    return start, end


def previous_utc_day() -> str:
    return (utc_now().date() - timedelta(days=1)).isoformat()


def round_half_up(value: float, digits: int = 0) -> float:
    quant = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def finite_float(value: str, label: str = "数值") -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}不是有效数字：{value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label}必须是有限数字")
    return parsed


def fmt_number(value: object, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_datetime(value: object) -> str:
    if not value:
        return "—"
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1]
    text = text.replace("T", " ")
    if len(text) >= 16:
        return f"{text[:16]} UTC"
    return text


def hour_index(observed_at: str) -> int:
    """从规范小时字符串中取小时序号。"""

    return int(observed_at[11:13])


def hour_label(hour: int) -> str:
    return f"{hour:02d}:00"


def time_to_hour(value: time) -> int:
    return value.hour
