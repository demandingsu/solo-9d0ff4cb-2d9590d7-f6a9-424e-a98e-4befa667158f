"""站号互换：登记、存量搬移、导入归站、幂等、重叠与并发语义。"""

from __future__ import annotations

import pytest

from app import db
from app.services import (
    ServiceError,
    SwapOverlapError,
    effective_station_id,
    import_observations,
    publish_report,
    register_station_swap,
    save_station,
)

DAY = "2026-11-01"
NEXT_DAY = "2026-11-02"
START = f"{DAY}T00:00:00Z"
END = f"{NEXT_DAY}T00:00:00Z"
POLLUTANTS = ["PM2.5", "PM10", "O3", "SO2", "NO2", "CO"]


def wide_csv(day: str = DAY, value: float = 20.0, hours=range(24)) -> bytes:
    rows = ["time," + ",".join(POLLUTANTS)]
    for hour in hours:
        rows.append(f"{day}T{hour:02d}:00:00Z," + ",".join([str(value)] * 6))
    return ("\n".join(rows) + "\n").encode("utf-8")


def two_day_csv() -> bytes:
    rows = ["time," + ",".join(POLLUTANTS)]
    for day, value in ((DAY, 20.0), (NEXT_DAY, 30.0)):
        for hour in range(24):
            rows.append(f"{day}T{hour:02d}:00:00Z," + ",".join([str(value)] * 6))
    return ("\n".join(rows) + "\n").encode("utf-8")


@pytest.fixture()
def pair(db_path):
    a = save_station("SWAPA", "互换甲站", 39.9, 116.4)
    b = save_station("SWAPB", "互换乙站", 31.2, 121.5)
    return a, b


def _import_ids(station_id: int) -> set[int]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT import_id FROM observations WHERE station_id = ?", (station_id,)
        ).fetchall()
    return {int(row["import_id"]) for row in rows}


# ---------------------------------------------------------------- 存量搬移


def test_swap_moves_existing_readings_both_ways_and_keeps_batch(pair):
    a, b = pair
    imp_a = import_observations(a["id"], wide_csv(DAY, 20.0), "a.csv")
    imp_b = import_observations(b["id"], wide_csv(DAY, 60.0, hours=range(12)), "b.csv")

    outcome = register_station_swap(a["id"], b["id"], START, END, note="网关贴反")

    assert outcome["created"] is True
    assert outcome["moved_a_to_b"] == 144
    assert outcome["moved_b_to_a"] == 72
    # A 只剩从 B 搬来的 12 小时（值 60）
    a_data = db.observations_for_day(a["id"], DAY)
    assert len(a_data["PM2.5"]) == 12
    assert set(a_data["PM2.5"].values()) == {60.0}
    # B 拿到 A 的完整 24 小时（值 20）
    b_data = db.observations_for_day(b["id"], DAY)
    assert len(b_data["PM2.5"]) == 24
    assert set(b_data["PM2.5"].values()) == {20.0}
    # 各自的导入批次号保留
    assert _import_ids(a["id"]) == {imp_b.import_id}
    assert _import_ids(b["id"]) == {imp_a.import_id}


def test_swap_single_sided_still_moves(pair):
    """即使只有一边有值，时段内的读数也要移过去。"""

    a, b = pair
    import_observations(a["id"], wide_csv(DAY, 20.0), "a.csv")
    outcome = register_station_swap(a["id"], b["id"], START, END)
    assert outcome["moved_a_to_b"] == 144
    assert outcome["moved_b_to_a"] == 0
    assert db.observations_for_day(a["id"], DAY) == {}
    assert len(db.observations_for_day(b["id"], DAY)["CO"]) == 24


def test_swap_exchanges_values_when_both_sides_have_same_hours(pair):
    """两站同一小时同一污染物都有值时直接对调，不触发唯一约束冲突。"""

    a, b = pair
    import_observations(a["id"], wide_csv(DAY, 20.0), "a.csv")
    import_observations(b["id"], wide_csv(DAY, 80.0), "b.csv")
    register_station_swap(a["id"], b["id"], START, END)
    assert set(db.observations_for_day(a["id"], DAY)["PM2.5"].values()) == {80.0}
    assert set(db.observations_for_day(b["id"], DAY)["PM2.5"].values()) == {20.0}


def test_swap_period_is_half_open(pair):
    """[start, end)：起点含、终点不含。"""

    a, b = pair
    rows = ["time," + ",".join(POLLUTANTS)]
    for hour in (22, 23):
        rows.append(f"{DAY}T{hour:02d}:00:00Z," + ",".join(["20"] * 6))
    for hour in (0, 1):
        rows.append(f"{NEXT_DAY}T{hour:02d}:00:00Z," + ",".join(["30"] * 6))
    import_observations(a["id"], ("\n".join(rows) + "\n").encode(), "edge.csv")

    register_station_swap(a["id"], b["id"], f"{DAY}T23:00:00Z", f"{NEXT_DAY}T01:00:00Z")

    # 23:00 与次日 00:00 在时段内 → 归 B；22:00 与次日 01:00 在时段外 → 留 A
    b_data = db.observations_for_day(b["id"], DAY)
    assert set(b_data["PM2.5"].keys()) == {f"{DAY}T23:00:00Z"}
    assert set(db.observations_for_day(b["id"], NEXT_DAY)["PM2.5"].keys()) == {
        f"{NEXT_DAY}T00:00:00Z"
    }
    assert set(db.observations_for_day(a["id"], DAY)["PM2.5"].keys()) == {f"{DAY}T22:00:00Z"}
    assert set(db.observations_for_day(a["id"], NEXT_DAY)["PM2.5"].keys()) == {
        f"{NEXT_DAY}T01:00:00Z"
    }


# ---------------------------------------------------------------- 导入归站


def test_import_after_swap_routes_rows_by_observation_time(pair):
    """登记后按原站号补传：时段内归对站，时段外照常入库。"""

    a, b = pair
    register_station_swap(a["id"], b["id"], START, END)
    outcome = import_observations(a["id"], two_day_csv(), "two_days.csv")
    # 只有 DAY 在误标时段内
    assert outcome.counts["remapped"] == 144
    assert outcome.counts["added"] == 288
    assert set(db.observations_for_day(b["id"], DAY)["PM2.5"].values()) == {20.0}
    assert db.observations_for_day(a["id"], DAY) == {}
    assert set(db.observations_for_day(a["id"], NEXT_DAY)["PM2.5"].values()) == {30.0}


def test_retransmission_to_original_station_matches_moved_rows(pair):
    """重传同一文件：时段内的行在对站命中“未变”，不重复入库。"""

    a, b = pair
    import_observations(a["id"], wide_csv(DAY, 20.0), "batch1.csv")
    register_station_swap(a["id"], b["id"], START, END)
    again = import_observations(a["id"], wide_csv(DAY, 20.0), "batch2.csv")
    assert again.counts["remapped"] == 144
    assert again.counts["unchanged"] == 144
    assert again.counts["added"] == 0
    assert len(db.observations_for_day(b["id"], DAY)["PM2.5"]) == 24


def test_import_reads_swap_rules_once_per_batch(pair, monkeypatch):
    """整批导入共用同一套已提交规则：事务内只读取一次。"""

    a, b = pair
    register_station_swap(a["id"], b["id"], START, END)
    calls = []
    real_fetch = db.fetch_swaps

    def counting(conn):
        calls.append(1)
        return real_fetch(conn)

    monkeypatch.setattr(db, "fetch_swaps", counting)
    import_observations(a["id"], two_day_csv(), "two_days.csv")
    assert len(calls) == 1


def test_interleaved_registration_and_imports(pair):
    """登记与导入交错：登记前的批次被搬移，登记后的批次直接归对站。"""

    a, b = pair
    first = import_observations(a["id"], wide_csv(DAY, 20.0), "batch1.csv")
    assert first.counts["remapped"] == 0
    assert len(db.observations_for_day(a["id"], DAY)["PM2.5"]) == 24

    register_station_swap(a["id"], b["id"], START, END)
    # 登记前批次的读数已搬到 B，批次号保留
    assert db.observations_for_day(a["id"], DAY) == {}
    assert _import_ids(b["id"]) == {first.import_id}

    # 登记后补传更正值：直接落到 B 并记更正
    rows = ["time," + ",".join(POLLUTANTS)]
    rows.append(f"{DAY}T00:00:00Z," + ",".join(["99"] * 6))
    second = import_observations(a["id"], ("\n".join(rows) + "\n").encode(), "fix.csv")
    assert second.counts["remapped"] == 6
    assert second.counts["corrected"] == 6
    assert db.observations_for_day(b["id"], DAY)["PM2.5"][f"{DAY}T00:00:00Z"] == 99.0


def test_effective_station_id_mapping():
    rules = [{"station_a_id": 1, "station_b_id": 2, "start_at": START, "end_at": END}]
    assert effective_station_id(rules, 1, f"{DAY}T05:00:00Z") == 2
    assert effective_station_id(rules, 2, f"{DAY}T05:00:00Z") == 1
    assert effective_station_id(rules, 3, f"{DAY}T05:00:00Z") == 3
    assert effective_station_id(rules, 1, START) == 2  # 起点含
    assert effective_station_id(rules, 1, END) == 1  # 终点不含
    assert effective_station_id(rules, 1, "2026-10-31T23:00:00Z") == 1


# ---------------------------------------------------------------- 幂等与重叠


def test_duplicate_registration_applies_once(pair):
    a, b = pair
    import_observations(a["id"], wide_csv(DAY, 20.0), "a.csv")
    first = register_station_swap(a["id"], b["id"], START, END)
    assert first["created"] is True
    # 再次登记（含站序颠倒）：只生效一次，不会把读数换回去
    second = register_station_swap(a["id"], b["id"], START, END)
    reversed_order = register_station_swap(b["id"], a["id"], START, END)
    assert second["created"] is False
    assert reversed_order["created"] is False
    assert second["swap"]["id"] == first["swap"]["id"]
    assert len(db.list_swaps()) == 1
    assert db.observations_for_day(a["id"], DAY) == {}
    assert len(db.observations_for_day(b["id"], DAY)["PM2.5"]) == 24


def test_overlap_registration_rejected(pair):
    a, b = pair
    c = save_station("SWAPC", "互换丙站", 30.5, 114.3)
    register_station_swap(a["id"], b["id"], START, END)
    # 同一站对、时段平移后重叠
    with pytest.raises(SwapOverlapError, match="重叠"):
        register_station_swap(a["id"], b["id"], f"{DAY}T12:00:00Z", f"{NEXT_DAY}T12:00:00Z")
    # 只涉及 A 的规则重叠也拒绝
    with pytest.raises(SwapOverlapError, match="重叠"):
        register_station_swap(a["id"], c["id"], f"{DAY}T06:00:00Z", f"{NEXT_DAY}T06:00:00Z")
    # 只涉及 B 同理
    with pytest.raises(SwapOverlapError, match="重叠"):
        register_station_swap(b["id"], c["id"], START, END)
    # 相邻但首尾相接（不含终点）不算重叠
    adjacent = register_station_swap(a["id"], b["id"], END, "2026-11-03T00:00:00Z")
    assert adjacent["created"] is True
    # 与 C 的不重叠时段允许
    ok = register_station_swap(a["id"], c["id"], "2026-11-03T00:00:00Z", "2026-11-04T00:00:00Z")
    assert ok["created"] is True


def test_failed_registration_preserves_rules_and_attribution(pair):
    a, b = pair
    import_observations(a["id"], wide_csv(DAY, 20.0), "a.csv")
    register_station_swap(a["id"], b["id"], START, END)
    assert db.observations_for_day(a["id"], DAY) == {}

    # 重叠登记被拒绝：原规则与读数归属不变
    with pytest.raises(SwapOverlapError):
        register_station_swap(a["id"], b["id"], f"{DAY}T06:00:00Z", f"{NEXT_DAY}T06:00:00Z")
    assert len(db.list_swaps()) == 1
    assert db.observations_for_day(a["id"], DAY) == {}
    assert len(db.observations_for_day(b["id"], DAY)["PM2.5"]) == 24

    # 非法输入同样不产生任何变化
    with pytest.raises(ServiceError):
        register_station_swap(a["id"], a["id"], START, END)
    with pytest.raises(ServiceError):
        register_station_swap(a["id"], b["id"], END, START)
    assert len(db.list_swaps()) == 1


def test_swap_input_validation(pair):
    a, b = pair
    with pytest.raises(ServiceError, match="不同"):
        register_station_swap(a["id"], a["id"], START, END)
    with pytest.raises(ServiceError, match="起点必须早于终点"):
        register_station_swap(a["id"], b["id"], END, START)
    with pytest.raises(ServiceError, match="整点"):
        register_station_swap(a["id"], b["id"], f"{DAY}T00:30:00Z", END)
    with pytest.raises(ServiceError, match="不存在"):
        register_station_swap(a["id"], 9999, START, END)
    with pytest.raises(ServiceError, match="无效"):
        register_station_swap("x", b["id"], START, END)


def test_delete_station_cascades_swaps(pair):
    from app.services import delete_station

    a, b = pair
    register_station_swap(a["id"], b["id"], START, END)
    assert len(db.list_swaps()) == 1
    assert delete_station(b["id"]) is True
    assert db.list_swaps() == []
    assert db.list_swaps_for_station(a["id"]) == []


def test_migration_adds_swap_table_and_remapped_column(tmp_path, monkeypatch):
    """旧库（无 station_swaps、imports 无 rows_remapped）启动时自动迁移。"""

    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stations (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT,"
        " latitude REAL, longitude REAL, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE imports (id INTEGER PRIMARY KEY, station_id INTEGER, filename TEXT,"
        " sha256 TEXT, rows_total INTEGER, rows_valid INTEGER, rows_missing INTEGER,"
        " rows_added INTEGER, rows_corrected INTEGER, dates_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE threshold_tables (id INTEGER PRIMARY KEY, name TEXT, content_hash TEXT,"
        " csv_text TEXT, snapshot_json TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("AIR_DATABASE_PATH", str(path))
    db.init_db(seed_default_threshold=False)

    with db.get_conn() as check:
        tables = {row["name"] for row in check.execute("SELECT name FROM sqlite_master")}
        assert "station_swaps" in tables
        cols = {row["name"] for row in check.execute("PRAGMA table_info(imports)")}
        assert "rows_remapped" in cols


# ---------------------------------------------------------------- 日报与覆盖


def test_reports_use_corrected_data_published_snapshot_frozen(pair):
    a, b = pair
    import_observations(a["id"], wide_csv(DAY, 20.0), "a.csv")
    import_observations(b["id"], wide_csv(DAY, 80.0), "b.csv")
    v1 = publish_report(a["id"], DAY)
    assert v1.ok

    register_station_swap(a["id"], b["id"], START, END)

    # 站点页小时覆盖取纠正后的数据
    coverage = db.observation_coverage(a["id"], DAY)
    assert coverage["PM2.5"]["count"] == 24
    assert set(coverage["PM2.5"]["hours"].values()) == {80.0}
    # 新发布的日报用纠正后的数据
    v2 = publish_report(a["id"], DAY)
    assert v2.ok and v2.version == 2
    assert db.get_report(v2.report_id)["readings"]["pollutants"]["PM2.5"]["daily_value"] == 80.0
    # 已发布的 v1 仍是原快照
    assert db.get_report(v1.report_id)["readings"]["pollutants"]["PM2.5"]["daily_value"] == 20.0
    # B 站新发布取到 A 原来的数据
    vb = publish_report(b["id"], DAY)
    assert vb.ok
    assert db.get_report(vb.report_id)["readings"]["pollutants"]["PM2.5"]["daily_value"] == 20.0


def test_backfill_to_original_station_completes_peer(pair):
    """单边缺值 + 交错补传：B 只有半天，互换后用原站号补传，读数归到 A 使其补齐。"""

    a, b = pair
    import_observations(a["id"], wide_csv(DAY, 20.0), "a_full.csv")
    import_observations(b["id"], wide_csv(DAY, 60.0, hours=range(12)), "b_partial.csv")
    v1 = publish_report(a["id"], DAY)
    assert v1.ok

    register_station_swap(a["id"], b["id"], START, END)
    # A 只剩 12 小时，发布被拒绝
    assert not publish_report(a["id"], DAY).ok

    # 用原站号 B 补传 12–23 时：时段内读数归到 A
    outcome = import_observations(b["id"], wide_csv(DAY, 45.0, hours=range(12, 24)), "b_backfill.csv")
    assert outcome.counts["remapped"] == 72
    assert outcome.counts["added"] == 72

    # A 补齐后可发布，日均 (60×12 + 45×12) / 24 = 52.5
    v2 = publish_report(a["id"], DAY)
    assert v2.ok
    assert db.get_report(v2.report_id)["readings"]["pollutants"]["PM2.5"]["daily_value"] == 52.5
    # v1 快照保持 20.0
    assert db.get_report(v1.report_id)["readings"]["pollutants"]["PM2.5"]["daily_value"] == 20.0
    # B 拿到 A 的完整 24 小时，也可发布
    assert publish_report(b["id"], DAY).ok


# ---------------------------------------------------------------- HTTP 层


def _make_pair_via_api(client) -> tuple[int, int]:
    ra = client.post(
        "/api/stations",
        json={"code": "APIA", "name": "接口甲站", "latitude": 39.9, "longitude": 116.4},
    )
    rb = client.post(
        "/api/stations",
        json={"code": "APIB", "name": "接口乙站", "latitude": 31.2, "longitude": 121.5},
    )
    return ra.json()["station"]["id"], rb.json()["station"]["id"]


def test_api_swap_flow(client):
    a_id, b_id = _make_pair_via_api(client)
    client.post(
        f"/api/stations/{a_id}/observations",
        files={"file": ("day.csv", wide_csv(DAY, 20.0), "text/csv")},
    )

    r = client.post(
        "/api/swaps",
        json={"station_a_id": a_id, "station_b_id": b_id, "start_at": START, "end_at": END},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["moved_a_to_b"] == 144
    assert body["moved_b_to_a"] == 0

    # 重复登记（站序颠倒）只生效一次
    r2 = client.post(
        "/api/swaps",
        json={"station_a_id": b_id, "station_b_id": a_id, "start_at": START, "end_at": END},
    )
    assert r2.status_code == 200
    assert r2.json()["created"] is False

    # 重叠 → 409
    r3 = client.post(
        "/api/swaps",
        json={
            "station_a_id": a_id,
            "station_b_id": b_id,
            "start_at": f"{DAY}T12:00:00Z",
            "end_at": f"{NEXT_DAY}T12:00:00Z",
        },
    )
    assert r3.status_code == 409
    assert r3.json()["ok"] is False

    swaps = client.get("/api/swaps").json()["swaps"]
    assert len(swaps) == 1
    assert swaps[0]["station_a_code"] == "APIA"

    # 用原站号补传更正：全部归到对站
    r4 = client.post(
        f"/api/stations/{a_id}/observations",
        files={"file": ("fix.csv", wide_csv(DAY, 25.0), "text/csv")},
    )
    assert r4.json()["counts"]["remapped"] == 144
    assert r4.json()["counts"]["corrected"] == 144


def test_api_swap_rejects_invalid_input(client):
    a_id, b_id = _make_pair_via_api(client)
    r = client.post(
        "/api/swaps",
        json={"station_a_id": a_id, "station_b_id": b_id, "start_at": END, "end_at": START},
    )
    assert r.status_code == 400
    r2 = client.post(
        "/api/swaps",
        json={"station_a_id": a_id, "station_b_id": a_id, "start_at": START, "end_at": END},
    )
    assert r2.status_code == 400


def test_swap_pages_and_form(client):
    a_id, b_id = _make_pair_via_api(client)
    client.post(
        f"/api/stations/{a_id}/observations",
        files={"file": ("day.csv", wide_csv(DAY, 20.0), "text/csv")},
    )
    assert client.get("/swaps").status_code == 200

    # 网页表单（datetime-local 提交无时区，按 UTC 解释）
    r = client.post(
        "/swaps",
        data={
            "station_a_id": a_id,
            "station_b_id": b_id,
            "start_at": f"{DAY}T00:00",
            "end_at": f"{NEXT_DAY}T00:00",
            "note": "网关贴反",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/swaps?msg=" in r.headers["location"]

    swaps_page = client.get("/swaps")
    assert "APIA" in swaps_page.text and "APIB" in swaps_page.text
    station_page = client.get(f"/stations/{a_id}?date={DAY}")
    assert "站号互换规则" in station_page.text
    assert "APIA ⇄ APIB" in station_page.text

    # 表单重复提交 → 幂等提示，不重复生效
    r2 = client.post(
        "/swaps",
        data={
            "station_a_id": a_id,
            "station_b_id": b_id,
            "start_at": f"{DAY}T00:00",
            "end_at": f"{NEXT_DAY}T00:00",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert len(client.get("/api/swaps").json()["swaps"]) == 1

    # 表单提交重叠规则 → 重定向带错误
    r3 = client.post(
        "/swaps",
        data={
            "station_a_id": a_id,
            "station_b_id": b_id,
            "start_at": f"{DAY}T06:00",
            "end_at": f"{NEXT_DAY}T06:00",
        },
        follow_redirects=False,
    )
    assert r3.status_code == 303
    assert "/swaps?err=" in r3.headers["location"]
