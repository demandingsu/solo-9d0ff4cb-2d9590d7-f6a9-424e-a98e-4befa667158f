"""小时污染物 CSV 解析。"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass, field

from .config import MAX_IMPORT_ROWS, MAX_UPLOAD_BYTES, POLLUTANT_ALIASES
from .utils import HourFormatError, parse_utc_hour

TIME_ALIASES = {
    "time",
    "timestamp",
    "datetime",
    "date_time",
    "observed_at",
    "observed",
    "hour",
    "utc",
    "时间",
    "观测时间",
    "日期时间",
    "时刻",
}
STATION_CODE_ALIASES = {"station_code", "station", "site_code", "站点编码", "站点代码", "站点"}
STATION_NAME_ALIASES = {"station_name", "site_name", "站点名称", "站点名"}
LAT_ALIASES = {"latitude", "lat", "纬度"}
LON_ALIASES = {"longitude", "lon", "lng", "经度"}
POLLUTANT_HEADER_ALIASES = POLLUTANT_ALIASES
MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-", "--", "缺测", "缺失", "无"}


class ImportError_(ValueError):
    """CSV 整体不可导入。"""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass
class ParsedObservation:
    observed_at: str
    pollutant: str
    value: float
    row: int


@dataclass
class ImportParseResult:
    observations: list[ParsedObservation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_total: int = 0
    rows_valid: int = 0
    rows_missing: int = 0
    naive_time_rows: int = 0
    station_code: str = ""
    station_name: str = ""
    latitude: float | None = None
    longitude: float | None = None
    dates: list[str] = field(default_factory=list)


def _clean_header(name: str) -> str:
    return name.replace("﻿", "").strip()


def _resolve_header(name: str) -> str:
    cleaned = _clean_header(name)
    # 允许 "PM2.5(µg/m³)"、"PM2.5 ug/m3" 这类带单位的列名。
    stripped = re.sub(r"[（(].*?[）)]", "", cleaned).strip()
    if stripped != cleaned:
        try:
            pollutant = _normalize_pollutant_header(stripped)
            return pollutant
        except ValueError:
            cleaned = stripped
    key = cleaned.lower().replace(" ", "").replace("_", "")
    if cleaned in POLLUTANT_HEADER_ALIASES:
        return POLLUTANT_HEADER_ALIASES[cleaned]
    if key in POLLUTANT_HEADER_ALIASES:
        return POLLUTANT_HEADER_ALIASES[key]
    lowered = cleaned.lower()
    if lowered in {"pollutant", "pollutant_name", "污染物", "指标", "项目"}:
        return "pollutant"
    if lowered in {"value", "concentration", "浓度", "数值", "测量值"}:
        return "value"
    if lowered in TIME_ALIASES or key in {alias.replace("_", "") for alias in TIME_ALIASES}:
        return "__time__"
    if lowered in STATION_CODE_ALIASES or key in {a.replace("_", "") for a in STATION_CODE_ALIASES}:
        return "__station_code__"
    if lowered in STATION_NAME_ALIASES or key in {a.replace("_", "") for a in STATION_NAME_ALIASES}:
        return "__station_name__"
    if lowered in LAT_ALIASES:
        return "__latitude__"
    if lowered in LON_ALIASES:
        return "__longitude__"
    raise ImportError_(f"无法识别的 CSV 列：{name!r}")


def parse_float(raw: str, allow_negative: bool = False) -> float | None:
    text = (raw or "").strip()
    if text.lower() in MISSING_TOKENS:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"数值无法解析：{raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"数值必须有限：{raw!r}")
    if value < 0 and not allow_negative:
        raise ValueError(f"浓度不能为负：{raw!r}")
    return value


def decode_upload(content: bytes, filename: str) -> str:
    if len(content) > MAX_UPLOAD_BYTES:
        raise ImportError_(f"文件过大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not content.strip():
        raise ImportError_("文件为空")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_("无法识别文件编码，请使用 UTF-8 或 GB18030 保存 CSV")


def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:5])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        return ","


def parse_observation_csv(text: str) -> ImportParseResult:
    """解析宽表（time + 污染物列）或长表（time,pollutant,value）。"""

    if not text.strip():
        raise ImportError_("文件为空")

    delimiter = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise ImportError_("缺少表头") from exc

    header = [_clean_header(name) for name in raw_header]
    if not any(header):
        raise ImportError_("表头为空")
    try:
        resolved = [_resolve_header(name) for name in header]
    except ImportError_ as exc:
        raise ImportError_(str(exc)) from exc

    if len(set(resolved)) != len(resolved):
        duplicates = sorted({name for name in resolved if resolved.count(name) > 1})
        readable = ["time" if item == "__time__" else item for item in duplicates]
        raise ImportError_("表头存在重复列：" + "、".join(readable))

    if "__time__" not in resolved:
        raise ImportError_("CSV 缺少时间列（time / timestamp / 观测时间）")

    result = ImportParseResult()
    known_special = {
        "__time__",
        "__station_code__",
        "__station_name__",
        "__latitude__",
        "__longitude__",
        "pollutant",
        "value",
    }
    long_form = "pollutant" in resolved and "value" in resolved

    if long_form:
        pollutant_columns: list[str] = []
    else:
        pollutant_columns = [name for name in resolved if name not in known_special]
        if not pollutant_columns:
            raise ImportError_(
                "CSV 未包含污染物列；支持宽表（time, PM2.5, PM10...）或长表（time, pollutant, value）"
            )

    for row_index, raw_row in enumerate(reader, start=2):
        if not raw_row or not any(cell.strip() for cell in raw_row):
            continue
        result.rows_total += 1
        if result.rows_total > MAX_IMPORT_ROWS:
            raise ImportError_(f"数据行超过上限 {MAX_IMPORT_ROWS} 行，请拆分文件")
        if len(raw_row) != len(resolved):
            result.errors.append(
                f"第 {row_index} 行列数不一致：表头 {len(resolved)} 列，实际 {len(raw_row)} 列"
            )
            result.rows_missing += 1
            continue
        record = dict(zip(resolved, raw_row))

        station_code = (record.get("__station_code__") or "").strip()
        station_name = (record.get("__station_name__") or "").strip()
        if station_code and not result.station_code:
            result.station_code = station_code
        if station_name and not result.station_name:
            result.station_name = station_name
        for key, alias in (("__latitude__", "latitude"), ("__longitude__", "longitude")):
            raw_value = (record.get(key) or "").strip()
            if raw_value and getattr(result, alias) is None:
                try:
                    setattr(result, alias, parse_float(raw_value, allow_negative=True))
                except ValueError:
                    result.warnings.append(f"第 {row_index} 行 {alias} 无效，已忽略：{raw_value!r}")

        raw_time = (record.get("__time__") or "").strip()
        if not raw_time:
            result.errors.append(f"第 {row_index} 行：时间为空")
            result.rows_missing += 1
            continue
        try:
            observed_at, naive = parse_utc_hour(raw_time)
        except HourFormatError as exc:
            result.errors.append(f"第 {row_index} 行：{exc}（{raw_time!r}）")
            result.rows_missing += 1
            continue
        if naive:
            result.naive_time_rows += 1
        if observed_at[:10] not in result.dates:
            result.dates.append(observed_at[:10])

        if long_form:
            pollutant_raw = (record.get("pollutant") or "").strip()
            value_raw = (record.get("value") or "").strip()
            if not pollutant_raw:
                result.errors.append(f"第 {row_index} 行：污染物为空")
                result.rows_missing += 1
                continue
            try:
                pollutant = _normalize_pollutant_header(pollutant_raw)
            except ValueError as exc:
                result.errors.append(f"第 {row_index} 行：{exc}")
                result.rows_missing += 1
                continue
            pairs = [(pollutant, value_raw)]
        else:
            pairs = [(name, record.get(name, "")) for name in pollutant_columns]

        row_missing = 0
        imported_any = False
        for pollutant, value_raw in pairs:
            try:
                value = parse_float(value_raw)
            except ValueError as exc:
                result.errors.append(f"第 {row_index} 行 {pollutant}：{exc}")
                row_missing += 1
                continue
            if value is None:
                row_missing += 1
                continue
            result.observations.append(
                ParsedObservation(
                    observed_at=observed_at, pollutant=pollutant, value=value, row=row_index
                )
            )
            imported_any = True

        if long_form and not pairs:
            row_missing += 1
        if imported_any:
            result.rows_valid += 1
        else:
            result.rows_missing += 1

    if result.rows_total == 0:
        raise ImportError_("CSV 没有数据行")
    if not result.observations:
        first_errors = result.errors[:5]
        detail = ("：" + "；".join(first_errors)) if first_errors else "：全部行为空或缺测"
        raise ImportError_(
            "没有可导入的有效观测值" + detail,
            errors=result.errors[:20],
        )

    result.dates.sort()
    if result.naive_time_rows:
        result.warnings.append(
            f"{result.naive_time_rows} 行时间未标注时区，已按 UTC 解释"
        )
    return result


def _normalize_pollutant_header(raw: str) -> str:
    value = (raw or "").strip()
    key = value.lower().replace(" ", "")
    if value in POLLUTANT_ALIASES:
        return POLLUTANT_ALIASES[value]
    if key in POLLUTANT_ALIASES:
        return POLLUTANT_ALIASES[key]
    pretty = {
        "pm2.5": "PM2.5",
        "pm25": "PM2.5",
        "pm10": "PM10",
        "o3": "O3",
        "so2": "SO2",
        "no2": "NO2",
        "co": "CO",
    }
    if key in pretty:
        return pretty[key]
    raise ValueError(f"未知污染物 {raw!r}")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def format_import_summary(result: ImportParseResult) -> str:
    parts = [f"读取 {result.rows_total} 行", f"有效 {result.rows_valid} 行"]
    if result.rows_missing:
        parts.append(f"缺测/无效 {result.rows_missing} 行")
    return "，".join(parts)
