"""日报计算引擎测试：均值、滚动窗口、完整性与首要污染物。"""

from __future__ import annotations

import pytest

from app.calc import evaluate_day
from app.config import DEFAULT_THRESHOLD_CSV
from app.thresholds import parse_threshold_csv

DAY = "2026-11-01"


@pytest.fixture()
def table():
    return parse_threshold_csv(DEFAULT_THRESHOLD_CSV, source="test")


def full_series(value: float = 20.0) -> dict[str, dict[str, float]]:
    hours = {f"{DAY}T{hour:02d}:00:00Z": value for hour in range(24)}
    return {key: dict(hours) for key in ["PM2.5", "PM10", "O3", "SO2", "NO2", "CO"]}


def test_complete_day_publishes(table):
    result = evaluate_day(table, full_series(), DAY)
    assert result.ok, result.reasons
    for key in ["PM2.5", "PM10", "O3", "SO2", "NO2", "CO"]:
        detail = result.pollutants[key]
        assert detail["sufficient"] is True
        assert detail["valid_hours"] == 24
        assert detail["daily_value"] == pytest.approx(20.0)
        assert detail["iaqi"] is not None


def test_pm25_mean_uses_available_hours(table):
    series = full_series()
    # 18 个有效小时，全部为 35 → 均值 35 → IAQI 50。
    series["PM2.5"] = {f"{DAY}T{hour:02d}:00:00Z": 35.0 for hour in range(18)}
    result = evaluate_day(table, series, DAY)
    assert result.ok, result.reasons
    assert result.pollutants["PM2.5"]["valid_hours"] == 18
    assert result.pollutants["PM2.5"]["daily_value"] == 35.0
    assert result.pollutants["PM2.5"]["iaqi"] == 50


def test_pm25_with_17_hours_is_rejected(table):
    series = full_series()
    series["PM2.5"] = {f"{DAY}T{hour:02d}:00:00Z": 35.0 for hour in range(17)}
    result = evaluate_day(table, series, DAY)
    assert not result.ok
    assert any("PM2.5" in reason and "17/24" in reason for reason in result.reasons)
    assert result.pollutants["PM2.5"]["sufficient"] is False
    assert result.pollutants["PM2.5"]["daily_value"] is None
    assert result.max_iaqi is None
    assert result.readings_snapshot  # 失败仍保留读数快照供排查


def test_missing_pollutant_entirely_is_rejected(table):
    series = full_series()
    del series["CO"]
    result = evaluate_day(table, series, DAY)
    assert not result.ok
    assert any(reason.startswith("CO 缺测") for reason in result.reasons)


def test_all_six_required_for_publication(table):
    series = full_series()
    series["NO2"] = {}
    result = evaluate_day(table, series, DAY)
    assert not result.ok
    assert result.completeness["NO2"]["sufficient"] is False


def test_o3_rolling_window_picks_max_mean(table):
    series = full_series()
    series["O3"] = {}
    # 08–15 时为高值 200，其余为 50。8 小时窗口最大均值应正好是 200。
    for hour in range(24):
        value = 200.0 if 8 <= hour <= 15 else 50.0
        series["O3"][f"{DAY}T{hour:02d}:00:00Z"] = value
    result = evaluate_day(table, series, DAY)
    assert result.ok, result.reasons
    detail = result.pollutants["O3"]
    assert detail["complete_windows"] == 17
    assert detail["best_window"]["start"] == f"{DAY}T08:00:00Z"
    assert detail["best_window"]["end"] == f"{DAY}T15:00:00Z"
    assert detail["daily_value"] == pytest.approx(200.0)


def test_o3_window_spanning_day_boundary_not_used(table):
    """O₃ 8 小时窗口只能落在本 UTC 日内，不能用前一日 23 时补齐。"""

    series = full_series()
    series["O3"] = {}
    # 只有 00–07 时有值，恰好是一个完整窗口；但如果拿前一日数据补齐则会产生额外的跨日窗口。
    for hour in range(8):
        series["O3"][f"{DAY}T{hour:02d}:00:00Z"] = 100.0
    result = evaluate_day(table, series, DAY)
    assert result.ok, result.reasons
    detail = result.pollutants["O3"]
    assert detail["complete_windows"] == 1
    assert detail["best_window"]["start"] == f"{DAY}T00:00:00Z"
    assert detail["best_window"]["end"] == f"{DAY}T07:00:00Z"
    assert detail["valid_hours"] == 8


def test_o3_with_seven_hours_is_rejected(table):
    series = full_series()
    series["O3"] = {f"{DAY}T{hour:02d}:00:00Z": 100.0 for hour in range(7)}
    result = evaluate_day(table, series, DAY)
    assert not result.ok
    assert any("O3" in reason or "O₃" in reason for reason in result.reasons)
    assert result.pollutants["O3"]["complete_windows"] == 0
    assert result.pollutants["O3"]["daily_value"] is None


def test_o3_gap_breaks_windows(table):
    series = full_series()
    series["O3"] = {f"{DAY}T{hour:02d}:00:00Z": 100.0 for hour in range(24) if hour != 12}
    result = evaluate_day(table, series, DAY)
    assert result.ok, result.reasons
    detail = result.pollutants["O3"]
    # 缺 12 时，包含 12 时的窗口全部不完整：剩余 00–04 与 19–23 起点的窗口。
    # 起点 0..4（5 个）和 13..16（4 个）以及……精确计算：完整窗口起点为 0-4 与 13-16。
    assert detail["complete_windows"] == 9
    hours = {f"{DAY}T{hour:02d}:00:00Z" for hour in range(24)}
    for window in [
        detail["best_window"],
    ]:
        assert window["start"] in hours
    # 最佳窗口不能覆盖 11–12 或 12–13 的缺测点。
    start_hour = int(detail["best_window"]["start"][11:13])
    assert not (start_hour <= 12 <= start_hour + 7)


def test_o3_window_at_end_of_day(table):
    series = full_series()
    series["O3"] = {}
    for hour in range(16, 24):
        series["O3"][f"{DAY}T{hour:02d}:00:00Z"] = 150.0
    result = evaluate_day(table, series, DAY)
    assert result.ok
    assert result.pollutants["O3"]["best_window"]["start"] == f"{DAY}T16:00:00Z"
    assert result.pollutants["O3"]["daily_value"] == pytest.approx(150.0)


def test_peak_hourly_iaqi_for_so2_no2_co(table):
    series = full_series()
    series["NO2"] = {f"{DAY}T{hour:02d}:00:00Z": 10.0 for hour in range(24)}
    series["NO2"][f"{DAY}T14:00:00Z"] = 300.0  # 200–400 → 50–100：300 → 75
    result = evaluate_day(table, series, DAY)
    assert result.ok, result.reasons
    detail = result.pollutants["NO2"]
    assert detail["daily_value"] == 300.0
    assert detail["iaqi"] == 75
    assert detail["peak_hour"] == f"{DAY}T14:00:00Z"


def test_primary_pollutant_is_max_and_can_tie(table):
    series = full_series(20.0)
    # PM2.5 与 PM10 同为高值且 IAQI 相同（PM2.5 75 → 100，PM10 150 → 100）。
    series["PM2.5"] = {f"{DAY}T{hour:02d}:00:00Z": 75.0 for hour in range(24)}
    series["PM10"] = {f"{DAY}T{hour:02d}:00:00Z": 150.0 for hour in range(24)}
    # CO 20 mg/m³ 会达到 IAQI 120，压低以免干扰首要污染物断言。
    series["CO"] = {f"{DAY}T{hour:02d}:00:00Z": 2.0 for hour in range(24)}
    result = evaluate_day(table, series, DAY)
    assert result.ok
    assert result.max_iaqi == 100
    assert set(result.primary_pollutants) == {"PM2.5", "PM10"}
    assert result.primary_pollutant == "PM2.5、PM10"


def test_primary_pollutant_still_labelled_when_aqi_low(table):
    """需求要求始终标明最大分指数项；低 AQI 时页面另提示“优”。"""

    result = evaluate_day(table, full_series(5.0), DAY)
    assert result.ok
    assert result.max_iaqi <= 50
    assert result.primary_pollutant is not None
    assert result.level == "优"


def test_future_date_rejected(table):
    result = evaluate_day(table, full_series(), "2999-01-01")
    assert not result.ok
    assert any("不能晚于" in reason for reason in result.reasons)


def test_completeness_snapshot_records_missing_hours(table):
    series = full_series()
    series["PM2.5"].pop(f"{DAY}T05:00:00Z")
    result = evaluate_day(table, series, DAY)
    assert result.ok
    assert f"{DAY}T05:00:00Z" in result.completeness["PM2.5"]["missing_hours"]
    assert result.completeness["PM10"]["missing_hours"] == []
