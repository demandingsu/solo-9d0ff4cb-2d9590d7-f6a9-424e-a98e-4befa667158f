"""业务服务：导入、发布、查询与演示数据生成。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from . import db
from .calc import PublishResult, evaluate_day, readings_for_storage
from .config import DEFAULT_THRESHOLD_CSV, POLLUTANT_ORDER
from .importer import (
    ImportError_,
    decode_upload,
    parse_observation_csv,
    sha256_bytes,
)
from .thresholds import ThresholdError, ThresholdTable, parse_threshold_csv, table_from_snapshot
from .utils import HourFormatError, parse_utc_hour, round_half_up, utc_now


class ServiceError(ValueError):
    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or []


class SwapOverlapError(ServiceError):
    """互换时段与既有规则重叠，登记被拒绝。"""


@dataclass
class ImportOutcome:
    import_id: int
    station: dict
    counts: dict
    dates: list[str]
    warnings: list[str]
    errors: list[str]
    created_station: bool


def normalize_station_code(code: str) -> str:
    value = (code or "").strip()
    if not value:
        raise ServiceError("站点编码不能为空")
    if len(value) > 40:
        raise ServiceError("站点编码过长（最多 40 字符）")
    return value


def validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as exc:
        raise ServiceError("经纬度必须是数字") from exc
    if not -90 <= lat <= 90:
        raise ServiceError("纬度必须在 -90 到 90 之间")
    if not -180 <= lon <= 180:
        raise ServiceError("经度必须在 -180 到 180 之间")
    return lat, lon


def save_station(code: str, name: str, latitude: float, longitude: float) -> dict:
    code = normalize_station_code(code)
    name = (name or "").strip() or code
    if len(name) > 80:
        raise ServiceError("站点名称过长（最多 80 字符）")
    lat, lon = validate_coordinates(latitude, longitude)
    with db.transaction() as conn:
        station_id = db.upsert_station(conn, code, name, lat, lon)
    station = db.get_station(station_id)
    assert station is not None
    return station


def import_observations(
    station_id: int,
    content: bytes,
    filename: str,
    prefer_embedded_station: bool = False,
) -> ImportOutcome:
    station = db.get_station(station_id)
    if station is None:
        raise ServiceError("站点不存在")

    try:
        text = decode_upload(content, filename)
        parsed = parse_observation_csv(text)
    except ImportError_ as exc:
        raise ServiceError(str(exc), getattr(exc, "errors", [])) from exc

    if parsed.station_code and parsed.station_code != station["code"]:
        raise ServiceError(
            f"CSV 内站点编码 {parsed.station_code!r} 与所选站点 {station['code']!r} 不一致"
        )

    counts = {"total": parsed.rows_total, "valid": parsed.rows_valid, "missing": parsed.rows_missing}
    digest = sha256_bytes(content)

    added = unchanged = corrected = remapped = 0
    with db.transaction() as conn:
        # 整批导入共用同一套已提交互换规则：事务（BEGIN IMMEDIATE）内一次性读取，
        # 登记事务与导入事务互斥，批内不会混用新旧两套规则。
        swap_rules = db.fetch_swaps(conn)
        import_id = db.record_import(
            conn,
            station_id=station_id,
            filename=filename or "upload.csv",
            sha256=digest,
            counts={**counts, "added": 0, "corrected": 0, "remapped": 0},
            dates=parsed.dates,
        )
        for item in parsed.observations:
            # 网页与 API 仍按原站号接收；时段内的读数按观测时刻归到对站。
            target_station_id = effective_station_id(swap_rules, station_id, item.observed_at)
            if target_station_id != station_id:
                remapped += 1
            outcome = db.upsert_observation(
                conn,
                station_id=target_station_id,
                observed_at=item.observed_at,
                pollutant=item.pollutant,
                value=item.value,
                import_id=import_id,
            )
            if outcome == "added":
                added += 1
            elif outcome == "corrected":
                corrected += 1
            else:
                unchanged += 1
        counts.update(
            {"added": added, "corrected": corrected, "unchanged": unchanged, "remapped": remapped}
        )
        conn.execute(
            "UPDATE imports SET rows_added = ?, rows_corrected = ?, rows_remapped = ?,"
            " rows_total = ?, rows_valid = ?, rows_missing = ? WHERE id = ?",
            (
                added,
                corrected,
                remapped,
                counts["total"],
                counts["valid"],
                counts["missing"],
                import_id,
            ),
        )

    return ImportOutcome(
        import_id=import_id,
        station=station,
        counts=counts,
        dates=parsed.dates,
        warnings=parsed.warnings,
        errors=parsed.errors[:50],
        created_station=False,
    )


def save_threshold_upload(content: bytes, filename: str) -> dict:
    try:
        text = decode_upload(content, filename)
        table = parse_threshold_csv(text, source=filename or "上传")
    except ImportError_ as exc:
        raise ServiceError(str(exc)) from exc
    except ThresholdError as exc:
        raise ServiceError(str(exc)) from exc
    with db.transaction() as conn:
        table_id = db.save_threshold_table(conn, filename or "上传阈值", text, table.snapshot())
    return {"id": table_id, "table": table, "name": filename}


def load_thresholds(table_id: int | None) -> tuple[dict, ThresholdTable]:
    record = db.get_threshold_table(table_id)
    if record is None:
        raise ServiceError("阈值表不存在")
    table = table_from_snapshot(json.loads(record["snapshot_json"]))
    if not table.content_hash:
        table.content_hash = record["content_hash"]
    return record, table


@dataclass
class PublishOutcome:
    ok: bool
    report_id: int | None
    version: int | None
    result: PublishResult
    previous_report: dict | None
    failure_id: int | None = None


def publish_report(
    station_id: int,
    report_date: str,
    threshold_table_id: int | None = None,
    note: str = "",
) -> PublishOutcome:
    station = db.get_station(station_id)
    if station is None:
        raise ServiceError("站点不存在")
    try:
        day = date.fromisoformat(report_date)
        if day.isoformat() != report_date:
            raise ValueError
    except ValueError as exc:
        raise ServiceError("日报日期格式应为 YYYY-MM-DD") from exc

    record, table = load_thresholds(threshold_table_id)
    observations = db.observations_for_day(station_id, report_date)
    result = evaluate_day(table, observations, report_date)

    if not result.ok:
        with db.transaction() as conn:
            failure_id = db.record_publish_failure(
                conn,
                station_id=station_id,
                report_date=report_date,
                threshold_table_id=record["id"],
                threshold_hash=record["content_hash"],
                reasons=result.reasons,
                readings=result.readings_snapshot,
            )
        previous = db.get_current_report(station_id, report_date)
        return PublishOutcome(
            ok=False,
            report_id=None,
            version=None,
            result=result,
            previous_report=previous,
            failure_id=failure_id,
        )

    with db.transaction() as conn:
        db.resolve_failures(conn, station_id, report_date)
        report_id = db.create_report(
            conn,
            station_id=station_id,
            report_date=report_date,
            threshold_table_id=record["id"],
            threshold_hash=record["content_hash"],
            threshold_snapshot=table.snapshot(),
            readings=readings_for_storage(result),
            primary_pollutant=result.primary_pollutant or "",
            max_iaqi=int(result.max_iaqi or 0),
            aqi_level=result.level or "",
            completeness=result.completeness,
            note=note.strip(),
        )
    report = db.get_report(report_id)
    assert report is not None
    return PublishOutcome(
        ok=True,
        report_id=report_id,
        version=report["version"],
        result=result,
        previous_report=db.get_report(report["supersedes_id"]) if report["supersedes_id"] else None,
    )


def delete_station(station_id: int) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute("DELETE FROM stations WHERE id = ?", (station_id,))
        if cursor.rowcount:
            # 更正记录没有外键级联，显式清理，避免遗留孤儿数据。
            conn.execute(
                "DELETE FROM observation_revisions WHERE station_id = ?", (station_id,)
            )
    return cursor.rowcount > 0


# --------------------------------------------------------------- 站号互换


def effective_station_id(rules: list[dict], station_id: int, observed_at: str) -> int:
    """按已提交的互换规则，把（原站号, 观测时刻）映射到实际归属站点。

    重叠规则在登记时已被拒绝，因此任一（站点, 时刻）至多命中一条规则。
    """

    for rule in rules:
        if rule["start_at"] <= observed_at < rule["end_at"]:
            if station_id == rule["station_a_id"]:
                return rule["station_b_id"]
            if station_id == rule["station_b_id"]:
                return rule["station_a_id"]
    return station_id


def _parse_swap_hour(raw: str, label: str) -> str:
    try:
        hour, _naive = parse_utc_hour(str(raw or ""))
    except HourFormatError as exc:
        raise ServiceError(f"{label}：{exc}") from exc
    return hour


def register_station_swap(
    station_a_id: object,
    station_b_id: object,
    start_at: str,
    end_at: str,
    note: str = "",
) -> dict:
    """登记一对站点在误标时段内的站号互换。

    - 时段为 UTC 整点半开区间 [start_at, end_at)；
    - 同一对站点同一时段重复登记只生效一次（幂等返回既有规则，不重复搬移）；
    - 任一站的时段与其他规则重叠时拒绝登记；
    - 规则写入与存量读数搬移在同一事务内完成，任一步失败整体回滚。
    """

    try:
        a_id = int(station_a_id)  # type: ignore[arg-type]
        b_id = int(station_b_id)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ServiceError("站点编号无效") from exc
    if a_id == b_id:
        raise ServiceError("互换需要选择两个不同的站点")

    stations_by_id = {s["id"]: s for s in db.list_stations()}
    if a_id not in stations_by_id or b_id not in stations_by_id:
        raise ServiceError("站点不存在，请刷新后重试")

    start = _parse_swap_hour(start_at, "时段起点")
    end = _parse_swap_hour(end_at, "时段终点")
    if not start < end:
        raise ServiceError("时段起点必须早于终点（含起点、不含终点）")

    # 站对规范化（小 id 在前），互换语义对称，便于幂等判断与唯一约束。
    lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    note = (note or "").strip()[:200]

    def _code(sid: int) -> str:
        station = stations_by_id.get(sid)
        return station["code"] if station else f"#{sid}"

    with db.transaction() as conn:
        existing = db.find_swap(conn, lo, hi, start, end)
        if existing is not None:
            # 重复登记同一对站和时段：只生效一次，直接返回既有规则。
            swap_id = int(existing["id"])
            moved_a_to_b = int(existing["moved_a_to_b"])
            moved_b_to_a = int(existing["moved_b_to_a"])
            created = False
        else:
            for rule in db.fetch_swaps(conn):
                shared = {rule["station_a_id"], rule["station_b_id"]} & {lo, hi}
                if not shared:
                    continue
                if rule["start_at"] < end and start < rule["end_at"]:
                    station_hint = "、".join(_code(sid) for sid in sorted(shared))
                    raise SwapOverlapError(
                        f"站点 {station_hint} 的时段与既有规则重叠："
                        f"{_code(rule['station_a_id'])} ⇄ {_code(rule['station_b_id'])} "
                        f"{rule['start_at']} – {rule['end_at']}，登记被拒绝"
                    )
            swap_id = db.insert_swap(conn, lo, hi, start, end, note)
            moved_a_to_b, moved_b_to_a = db.swap_observations(conn, lo, hi, start, end)
            db.update_swap_counts(conn, swap_id, moved_a_to_b, moved_b_to_a)
            created = True

    swap = db.get_swap(swap_id)
    assert swap is not None
    return {
        "created": created,
        "swap": swap,
        "moved_a_to_b": moved_a_to_b,
        "moved_b_to_a": moved_b_to_a,
    }


# --------------------------------------------------------------- 演示数据


def _demo_station_code(existing: list[str]) -> str:
    index = 1
    while True:
        candidate = f"DEMO{index:02d}"
        if candidate not in existing:
            return candidate
        index += 1


def _demo_value(pollutant: str, day: date, hour: int, seed: int) -> float:
    """生成带日内周期的稳定演示浓度，保证结果可复现。"""

    import math

    phase = math.sin((hour + seed) / 24 * 2 * math.pi)
    wave = math.cos((hour * 3 + seed * 5) / 24 * math.pi)
    base = {
        "PM2.5": 46.0,
        "PM10": 82.0,
        "O3": 118.0,
        "SO2": 22.0,
        "NO2": 54.0,
        "CO": 1.6,
    }[pollutant]
    amplitude = {
        "PM2.5": 14.0,
        "PM10": 22.0,
        "O3": 55.0,
        "SO2": 8.0,
        "NO2": 18.0,
        "CO": 0.6,
    }[pollutant]
    daily = ((day.toordinal() + seed) % 5) * 0.8
    if pollutant == "O3":
        # 臭氧午后峰值。
        value = base + amplitude * math.sin(max(0.0, (hour - 6) / 12) * math.pi) + daily
    else:
        value = base + amplitude * (0.5 * phase + 0.5 * wave) + daily
    return round_half_up(max(value, 0.0), 1)


def create_demo_dataset(
    station_code: str,
    station_name: str,
    latitude: float,
    longitude: float,
    days: int = 3,
    end_date: str | None = None,
    missing_plan: dict[int, list[str]] | None = None,
) -> dict:
    """创建站点并导入最近若干天的完整演示观测。"""

    lat, lon = validate_coordinates(latitude, longitude)
    code = normalize_station_code(station_code)
    anchor = date.fromisoformat(end_date) if end_date else utc_now().date() - timedelta(days=1)
    dates = [(anchor - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]

    station = save_station(code, station_name, lat, lon)
    rows = ["time," + ",".join(POLLUTANT_ORDER)]
    missing_plan = missing_plan or {}
    for offset, day_text in enumerate(dates):
        day = date.fromisoformat(day_text)
        for hour in range(24):
            values = []
            for pollutant in POLLUTANT_ORDER:
                if hour in missing_plan.get(offset, []):
                    values.append("")
                else:
                    values.append(str(_demo_value(pollutant, day, hour, seed=len(code))))
            rows.append(f"{day_text}T{hour:02d}:00:00Z," + ",".join(values))
    content = "\n".join(rows).encode("utf-8")
    outcome = import_observations(station["id"], content, f"{code}_demo.csv")
    return {
        "station": station,
        "dates": dates,
        "import_id": outcome.import_id,
        "counts": outcome.counts,
    }


def create_demo_scenario() -> dict:
    """一键演示：稳定站点、带缺测站点、带更正的历史版本。"""

    base_day = utc_now().date() - timedelta(days=2)
    created = []

    complete = create_demo_dataset(
        station_code="DEMO01",
        station_name="演示站点·城东（完整）",
        latitude=31.2304,
        longitude=121.4737,
        days=3,
        end_date=(base_day + timedelta(days=1)).isoformat(),
    )
    created.append(complete)

    incomplete = create_demo_dataset(
        station_code="DEMO02",
        station_name="演示站点·城西（缺测）",
        latitude=31.2150,
        longitude=121.4300,
        days=3,
        end_date=(base_day + timedelta(days=1)).isoformat(),
        missing_plan={1: list(range(0, 7)) + list(range(20, 24))},
    )
    created.append(incomplete)

    return {
        "days": [base_day.isoformat(), (base_day + timedelta(days=1)).isoformat()],
        "stations": [item["station"] for item in created],
        "datasets": created,
    }


def default_thresholds() -> ThresholdTable:
    return parse_threshold_csv(DEFAULT_THRESHOLD_CSV, source="默认阈值")
