"""分段阈值表解析、校验与 IAQI 计算。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from .config import (
    PERIOD_ALIASES,
    POLLUTANT_ALIASES,
    POLLUTANTS,
    POLLUTANT_ORDER,
)

REQUIRED_COLUMNS = (
    "pollutant",
    "period",
    "concentration_low",
    "concentration_high",
    "index_low",
    "index_high",
)
OPTIONAL_COLUMNS = ("unit", "note")

COLUMN_ALIASES = {
    "pollutant": "pollutant",
    "污染物": "pollutant",
    "项目": "pollutant",
    "period": "period",
    "时段": "period",
    "周期": "period",
    "concentration_low": "concentration_low",
    "concentration_high": "concentration_high",
    "conc_low": "concentration_low",
    "conc_high": "concentration_high",
    "浓度下限": "concentration_low",
    "浓度上限": "concentration_high",
    "index_low": "index_low",
    "index_high": "index_high",
    "iaqi_low": "index_low",
    "iaqi_high": "index_high",
    "指数下限": "index_low",
    "指数上限": "index_high",
    "unit": "unit",
    "单位": "unit",
    "note": "note",
    "备注": "note",
}

UNIT_NORMALIZATION = {
    "ug/m3": "µg/m³",
    "μg/m³": "µg/m³",
    "µg/m³": "µg/m³",
    "ug/m³": "µg/m³",
    "µg/m3": "µg/m³",
    "微克/立方米": "µg/m³",
    "mg/m3": "mg/m³",
    "mg/m³": "mg/m³",
    "毫克/立方米": "mg/m³",
}

MAX_IAQI = 500.0


class ThresholdError(ValueError):
    """阈值表整体不合法。"""


class ThresholdRowError(ValueError):
    """阈值表某一行不合法。"""


@dataclass(frozen=True)
class ThresholdSegment:
    pollutant: str
    period: str
    concentration_low: float
    concentration_high: float
    index_low: float
    index_high: float
    unit: str
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "pollutant": self.pollutant,
            "period": self.period,
            "concentration_low": self.concentration_low,
            "concentration_high": self.concentration_high,
            "index_low": self.index_low,
            "index_high": self.index_high,
            "unit": self.unit,
            "note": self.note,
        }


@dataclass
class ThresholdTable:
    segments: dict[str, list[ThresholdSegment]] = field(default_factory=dict)
    source: str = "上传"
    content_hash: str = ""

    def ordered_rows(self) -> list[ThresholdSegment]:
        rows: list[ThresholdSegment] = []
        for key in POLLUTANT_ORDER:
            rows.extend(self.segments.get(key, []))
        return rows

    def snapshot(self) -> dict:
        return {
            "source": self.source,
            "content_hash": self.content_hash,
            "max_index": MAX_IAQI,
            "pollutants": {
                key: [segment.as_dict() for segment in self.segments[key]]
                for key in POLLUTANT_ORDER
                if key in self.segments
            },
        }

    def canonical_json(self) -> str:
        payload = {"source": self.source, "rows": [row.as_dict() for row in self.ordered_rows()]}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def iaqi(self, pollutant: str, concentration: float) -> tuple[int, bool]:
        """按分段阈值计算 IAQI。

        返回 (IAQI 整数, 是否超出表上限而被截断)。
        低于 0 的浓度不接受；高于最高浓度断点时按该断点指数报告。
        """

        if pollutant not in self.segments:
            raise ThresholdError(f"阈值表缺少 {pollutant} 的分段")
        if not math.isfinite(concentration) or concentration < 0:
            raise ThresholdError(f"{pollutant} 浓度必须是非负有限数字")

        segments = self.segments[pollutant]
        top = segments[-1]
        if concentration >= top.concentration_high:
            # 恰好等于最高浓度断点时仍按该段指数报告，只有真正超出才算封顶。
            return int(round_half_up(top.index_high)), concentration > top.concentration_high
        if concentration <= segments[0].concentration_low:
            return int(round_half_up(segments[0].index_low)), False

        for segment in segments:
            if segment.concentration_low <= concentration <= segment.concentration_high:
                span = segment.concentration_high - segment.concentration_low
                if span == 0:
                    return int(round_half_up(segment.index_high)), False
                ratio = (concentration - segment.concentration_low) / span
                value = segment.index_low + ratio * (segment.index_high - segment.index_low)
                bounded = min(max(value, segment.index_low), segment.index_high)
                return int(round_half_up(bounded)), False
        # 理论上不可达；gap 已在校验时拒绝。
        raise ThresholdError(f"{pollutant} 浓度 {concentration} 不在任何分段内")


def round_half_up(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _normalize_header(name: str) -> str:
    cleaned = name.replace("﻿", "").strip()
    mapped = COLUMN_ALIASES.get(cleaned) or COLUMN_ALIASES.get(cleaned.lower())
    return mapped or cleaned.lower()


def _normalize_pollutant(raw: str) -> str:
    value = (raw or "").strip()
    key = value.lower().replace(" ", "")
    if value in POLLUTANT_ALIASES:
        return POLLUTANT_ALIASES[value]
    if key in POLLUTANT_ALIASES:
        return POLLUTANT_ALIASES[key]
    pretty = {"pm2.5": "PM2.5", "pm25": "PM2.5", "pm10": "PM10", "o3": "O3", "so2": "SO2", "no2": "NO2", "co": "CO"}
    if key in pretty:
        return pretty[key]
    raise ThresholdRowError(f"未知污染物 {raw!r}，支持 PM2.5、PM10、O3、SO2、NO2、CO")


def _normalize_period(raw: str) -> str:
    value = (raw or "").strip()
    key = value.lower().replace(" ", "")
    if key in PERIOD_ALIASES:
        return PERIOD_ALIASES[key]
    raise ThresholdRowError(f"未知时段 {raw!r}，支持 1h、8h、24h")


def _normalize_unit(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    key = value.lower().replace(" ", "")
    return UNIT_NORMALIZATION.get(key, value)


def _number(raw: str, label: str) -> float:
    text = (raw or "").strip()
    if not text:
        raise ThresholdRowError(f"{label}不能为空")
    try:
        value = float(text)
    except ValueError as exc:
        raise ThresholdRowError(f"{label}不是有效数字：{raw!r}") from exc
    if not math.isfinite(value):
        raise ThresholdRowError(f"{label}必须是有限数字")
    return value


def parse_threshold_csv(text: str, source: str = "上传") -> ThresholdTable:
    """解析并严格校验阈值 CSV。任何一行不合法都会整体拒绝。"""

    if not text or not text.strip():
        raise ThresholdError("阈值表为空")

    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise ThresholdError("阈值表缺少表头") from exc

    header = [_normalize_header(name) for name in raw_header]
    if len(set(header)) != len(header):
        raise ThresholdError("表头存在重复列")
    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise ThresholdError("阈值表缺少必需列：" + "、".join(missing))
    unknown = [name for name in header if name not in REQUIRED_COLUMNS + OPTIONAL_COLUMNS]
    if unknown:
        raise ThresholdError("阈值表包含未知列：" + "、".join(unknown))

    grouped: dict[str, list[ThresholdSegment]] = {}
    row_count = 0
    for line_no, raw_row in enumerate(reader, start=2):
        if not raw_row or not any(cell.strip() for cell in raw_row):
            continue
        row_count += 1
        if len(raw_row) != len(header):
            raise ThresholdError(f"第 {line_no} 行列数不对：表头 {len(header)} 列，实际 {len(raw_row)} 列")
        record = dict(zip(header, [cell.strip() for cell in raw_row]))
        try:
            pollutant = _normalize_pollutant(record["pollutant"])
            period = _normalize_period(record["period"])
            expected = POLLUTANTS[pollutant].period
            if period != expected:
                raise ThresholdRowError(
                    f"{pollutant} 的时段必须为 {expected}，实际为 {period}"
                )
            c_low = _number(record["concentration_low"], "浓度下限")
            c_high = _number(record["concentration_high"], "浓度上限")
            i_low = _number(record["index_low"], "指数下限")
            i_high = _number(record["index_high"], "指数上限")
            unit = _normalize_unit(record.get("unit", ""))
            expected_unit = POLLUTANTS[pollutant].unit
            if unit and unit != expected_unit:
                raise ThresholdRowError(
                    f"{pollutant} 的单位应为 {expected_unit}，实际为 {unit}"
                )
            if c_low < 0 or c_high <= c_low:
                raise ThresholdRowError("浓度区间必须满足 0 ≤ 下限 < 上限")
            if i_low < 0 or i_high <= i_low:
                raise ThresholdRowError("指数区间必须满足 0 ≤ 下限 < 上限")
            if i_high > MAX_IAQI:
                raise ThresholdRowError(f"指数上限不能超过 {int(MAX_IAQI)}")
        except ThresholdRowError as exc:
            raise ThresholdError(f"第 {line_no} 行：{exc}") from exc

        grouped.setdefault(pollutant, []).append(
            ThresholdSegment(
                pollutant=pollutant,
                period=period,
                concentration_low=c_low,
                concentration_high=c_high,
                index_low=i_low,
                index_high=i_high,
                unit=expected_unit,
                note=record.get("note", ""),
            )
        )

    if row_count == 0:
        raise ThresholdError("阈值表没有数据行")

    missing_pollutants = [key for key in POLLUTANT_ORDER if key not in grouped]
    if missing_pollutants:
        raise ThresholdError("阈值表缺少污染物：" + "、".join(missing_pollutants))

    for pollutant, segments in grouped.items():
        segments.sort(key=lambda item: item.concentration_low)
        if segments[0].concentration_low != 0:
            raise ThresholdError(f"{pollutant} 的第一段浓度下限必须为 0")
        if segments[0].index_low != 0:
            raise ThresholdError(f"{pollutant} 的第一段指数下限必须为 0")
        for previous, current in zip(segments, segments[1:]):
            if current.concentration_low != previous.concentration_high:
                raise ThresholdError(
                    f"{pollutant} 浓度断点不连续："
                    f"{previous.concentration_high:g} 之后为 {current.concentration_low:g}"
                )
            if current.index_low != previous.index_high:
                raise ThresholdError(
                    f"{pollutant} 指数断点不连续："
                    f"{previous.index_high:g} 之后为 {current.index_low:g}"
                )
        grouped[pollutant] = segments

    table = ThresholdTable(segments=grouped, source=source)
    table.content_hash = table.compute_hash()
    return table


def table_from_snapshot(snapshot: dict) -> ThresholdTable:
    """从已保存的快照恢复阈值表，用于复核历史版本。"""

    grouped: dict[str, list[ThresholdSegment]] = {}
    for pollutant, rows in snapshot.get("pollutants", {}).items():
        grouped[pollutant] = [ThresholdSegment(**row) for row in rows]
    table = ThresholdTable(
        segments=grouped,
        source=snapshot.get("source", "快照"),
        content_hash=snapshot.get("content_hash", ""),
    )
    return table
