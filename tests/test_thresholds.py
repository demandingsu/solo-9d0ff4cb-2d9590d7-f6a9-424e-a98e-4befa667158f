"""阈值表解析与 IAQI 计算测试。"""

from __future__ import annotations

import pytest

from app.config import DEFAULT_THRESHOLD_CSV
from app.thresholds import (
    ThresholdError,
    parse_threshold_csv,
    round_half_up,
)


@pytest.fixture()
def table():
    return parse_threshold_csv(DEFAULT_THRESHOLD_CSV, source="test")


def test_default_threshold_table_is_valid(table):
    assert set(table.segments) == {"PM2.5", "PM10", "O3", "SO2", "NO2", "CO"}
    assert len(table.ordered_rows()) == 41
    assert table.content_hash and len(table.content_hash) == 64


def test_threshold_hash_is_stable(table):
    again = parse_threshold_csv(DEFAULT_THRESHOLD_CSV, source="test")
    assert again.content_hash == table.content_hash

    # 仅改备注也应改变快照哈希（快照完整覆盖阈值内容）。
    changed = DEFAULT_THRESHOLD_CSV.replace(
        "PM2.5,24h,0,35,0,50,µg/m³,一级", "PM2.5,24h,0,35,0,50,µg/m³,一级-修订"
    )
    assert parse_threshold_csv(changed, source="test").content_hash != table.content_hash


def test_iaqi_interpolates_within_segment(table):
    # 35 → 50, 75 → 100：55 位于中点。
    assert table.iaqi("PM2.5", 55)[0] == 75
    assert table.iaqi("PM2.5", 0)[0] == 0
    assert table.iaqi("PM2.5", 35)[0] == 50
    assert table.iaqi("PM2.5", 75)[0] == 100


def test_iaqi_rounds_half_up(table):
    # PM10 50–150 → 50–100：101 的线性结果为 75.5，应向上取 76。
    assert table.iaqi("PM10", 101)[0] == 76
    assert round_half_up(75.5) == 76
    assert round_half_up(75.4) == 75


def test_iaqi_caps_above_top_breakpoint(table):
    value, capped = table.iaqi("PM2.5", 800)
    assert value == 500
    assert capped is True
    # 恰好等于最高断点不算封顶。
    value, capped = table.iaqi("PM2.5", 500)
    assert (value, capped) == (500, False)


def test_iaqi_rejects_negative(table):
    with pytest.raises(ThresholdError):
        table.iaqi("PM2.5", -1)


def test_unknown_pollutant_rejected(table):
    with pytest.raises(ThresholdError):
        table.iaqi("PM1", 10)


def test_threshold_missing_pollutant_is_rejected():
    csv_text = "\n".join(
        line for line in DEFAULT_THRESHOLD_CSV.splitlines() if not line.startswith("CO,1h")
    )
    with pytest.raises(ThresholdError, match="CO"):
        parse_threshold_csv(csv_text)


def test_threshold_gap_is_rejected():
    broken = DEFAULT_THRESHOLD_CSV.replace("PM2.5,24h,35,75,50,100", "PM2.5,24h,36,75,50,100")
    with pytest.raises(ThresholdError, match="不连续"):
        parse_threshold_csv(broken)


def test_threshold_wrong_period_is_rejected():
    broken = DEFAULT_THRESHOLD_CSV.replace("O3,8h,0,100", "O3,24h,0,100")
    with pytest.raises(ThresholdError, match="时段"):
        parse_threshold_csv(broken)


def test_threshold_wrong_unit_is_rejected():
    broken = DEFAULT_THRESHOLD_CSV.replace("CO,1h,0,5,0,50,mg/m³", "CO,1h,0,5,0,50,µg/m³")
    with pytest.raises(ThresholdError, match="单位"):
        parse_threshold_csv(broken)


def test_threshold_missing_column_is_rejected():
    lines = DEFAULT_THRESHOLD_CSV.splitlines()
    header = lines[0].replace(",index_high", "")
    stripped = [header] + [",".join(row.split(",")[:-2] + row.split(",")[-2:]) for row in lines[1:]]
    with pytest.raises(ThresholdError, match="缺少必需列"):
        parse_threshold_csv("\n".join(stripped))


def test_threshold_duplicate_headers_rejected():
    text = DEFAULT_THRESHOLD_CSV.replace("pollutant,period", "pollutant,pollutant,period", 1)
    with pytest.raises(ThresholdError, match="重复列"):
        parse_threshold_csv(text)


def test_threshold_empty_rejected():
    with pytest.raises(ThresholdError):
        parse_threshold_csv("   ")


def test_threshold_chinese_headers_accepted():
    text = DEFAULT_THRESHOLD_CSV.replace(
        "pollutant,period,concentration_low,concentration_high,index_low,index_high,unit,note",
        "污染物,时段,浓度下限,浓度上限,指数下限,指数上限,单位,备注",
    )
    table = parse_threshold_csv(text)
    assert table.iaqi("PM2.5", 55)[0] == 75
