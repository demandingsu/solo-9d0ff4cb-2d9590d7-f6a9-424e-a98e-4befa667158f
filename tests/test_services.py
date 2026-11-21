"""导入、发布、版本与失败保留的集成测试。"""

from __future__ import annotations

import pytest

from app import db
from app.config import DEFAULT_THRESHOLD_CSV
from app.importer import ImportError_, parse_observation_csv
from app.services import ServiceError, import_observations, publish_report, save_station

DAY = "2026-11-01"
POLLUTANTS = ["PM2.5", "PM10", "O3", "SO2", "NO2", "CO"]


def wide_csv(day: str = DAY, value: float = 20.0, overrides: dict | None = None) -> bytes:
    overrides = overrides or {}
    rows = ["time," + ",".join(POLLUTANTS)]
    for hour in range(24):
        cells = []
        for key in POLLUTANTS:
            if hour in overrides.get(key, {}):
                cell = overrides[key][hour]
                cells.append("" if cell is None else str(cell))
            else:
                cells.append(str(value))
        rows.append(f"{day}T{hour:02d}:00:00Z," + ",".join(cells))
    return ("\n".join(rows) + "\n").encode("utf-8")


@pytest.fixture()
def station(db_path):
    return save_station("TEST01", "测试站", 39.9, 116.4)


def test_import_writes_observations(station):
    outcome = import_observations(station["id"], wide_csv(), "day.csv")
    assert outcome.counts["added"] == 144
    assert outcome.counts["corrected"] == 0
    coverage = db.observation_coverage(station["id"], DAY)
    assert all(coverage[key]["count"] == 24 for key in POLLUTANTS)


def test_reimport_same_values_is_idempotent(station):
    import_observations(station["id"], wide_csv(), "day.csv")
    outcome = import_observations(station["id"], wide_csv(), "day.csv")
    assert outcome.counts["added"] == 0
    assert outcome.counts["corrected"] == 0
    assert outcome.counts["unchanged"] == 144


def test_reimport_changed_value_records_correction(station):
    import_observations(station["id"], wide_csv(), "day.csv")
    outcome = import_observations(
        station["id"], wide_csv(overrides={"PM2.5": {3: 99.0}}), "fix.csv"
    )
    assert outcome.counts["corrected"] == 1
    with db.get_conn() as conn:
        revisions = conn.execute(
            "SELECT * FROM observation_revisions WHERE station_id = ?", (station["id"],)
        ).fetchall()
    assert len(revisions) == 1
    assert revisions[0]["old_value"] == 20.0
    assert revisions[0]["new_value"] == 99.0


def test_import_missing_values_not_stored(station):
    outcome = import_observations(
        station["id"], wide_csv(overrides={"O3": {hour: None for hour in range(6)}}), "gap.csv"
    )
    assert outcome.counts["added"] == 144 - 6
    assert db.observation_coverage(station["id"], DAY)["O3"]["count"] == 18


def test_import_rejects_station_mismatch(station):
    rows = ["station_code,time," + ",".join(POLLUTANTS)]
    rows.append("OTHER01,2026-11-01T00:00:00Z," + ",".join(["10"] * 6))
    with pytest.raises(ServiceError, match="不一致"):
        import_observations(station["id"], ("\n".join(rows)).encode(), "x.csv")


def test_import_rejects_bad_time_alignment(station):
    rows = ["time," + ",".join(POLLUTANTS)]
    rows.append("2026-11-01T00:30:00Z," + ",".join(["10"] * 6))
    with pytest.raises(ServiceError, match="整点"):
        import_observations(station["id"], ("\n".join(rows)).encode(), "x.csv")


def test_import_accepts_long_form(station):
    rows = ["time,pollutant,value"]
    for hour in range(24):
        for key in POLLUTANTS:
            rows.append(f"{DAY}T{hour:02d}:00:00Z,{key},{20 + hour}")
    outcome = import_observations(station["id"], ("\n".join(rows)).encode(), "long.csv")
    assert outcome.counts["added"] == 144


def test_import_accepts_gbk_and_chinese_headers(station):
    header = "时间,PM2.5,PM10,O3,SO2,NO2,CO"
    rows = [header]
    for hour in range(24):
        rows.append(f"{DAY}T{hour:02d}:00:00Z," + ",".join(["20"] * 6))
    content = ("\n".join(rows)).encode("gb18030")
    outcome = import_observations(station["id"], content, "gbk.csv")
    assert outcome.counts["added"] == 144


def test_parse_naive_time_treated_as_utc():
    text = "time,PM2.5\n2026-11-01T00:00:00,12\n"
    parsed = parse_observation_csv(text)
    assert parsed.observations[0].observed_at == "2026-11-01T00:00:00Z"
    assert parsed.naive_time_rows == 1


def test_parse_unknown_column_rejected():
    text = "time,PM2.5,XYZ\n2026-11-01T00:00:00Z,10,1\n"
    with pytest.raises(ImportError_, match="无法识别"):
        parse_observation_csv(text)


def test_publish_creates_version_one(station):
    import_observations(station["id"], wide_csv(), "day.csv")
    outcome = publish_report(station["id"], DAY)
    assert outcome.ok
    assert outcome.version == 1
    report = db.get_report(outcome.report_id)
    assert report["is_current"] == 1
    assert report["threshold_snapshot"]["content_hash"] == report["threshold_hash"]
    assert set(report["readings"]["pollutants"]) == set(POLLUTANTS)


def test_republish_creates_new_version_and_keeps_snapshot(station):
    import_observations(station["id"], wide_csv(), "day.csv")
    first = publish_report(station["id"], DAY, note="初版")
    import_observations(station["id"], wide_csv(overrides={"PM2.5": {h: 60.0 for h in range(24)}}), "fix.csv")
    second = publish_report(station["id"], DAY, note="更正")

    assert second.version == 2
    assert second.report_id != first.report_id
    old = db.get_report(first.report_id)
    new = db.get_report(second.report_id)
    assert old["is_current"] == 0
    assert new["is_current"] == 1
    assert new["supersedes_id"] == first.report_id
    assert new["note"] == "更正"
    # 旧版本读数保持冻结，不随新观测变化。
    assert old["readings"]["pollutants"]["PM2.5"]["daily_value"] == 20.0
    assert new["readings"]["pollutants"]["PM2.5"]["daily_value"] == 60.0


def test_failed_publish_preserves_previous_complete_report(station):
    import_observations(station["id"], wide_csv(), "day.csv")
    first = publish_report(station["id"], DAY)
    assert first.ok

    # 用缺测数据覆盖（例如去掉了大部分小时值），再发布 → 必须失败。
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM observations WHERE station_id = ? AND observed_at >= ?",
            (station["id"], f"{DAY}T10:00:00Z"),
        )
    failed = publish_report(station["id"], DAY)
    assert not failed.ok
    assert failed.failure_id is not None
    assert any("缺测" in reason for reason in failed.result.reasons)
    assert failed.previous_report["id"] == first.report_id

    current = db.get_current_report(station["id"], DAY)
    assert current["id"] == first.report_id
    assert current["version"] == 1
    assert db.get_report(first.report_id)["is_current"] == 1
    failure = db.latest_failure(station["id"], DAY)
    assert failure and failure["reasons"]


def test_successful_republish_resolves_failure(station):
    import_observations(station["id"], wide_csv(overrides={"CO": {h: None for h in range(24)}}), "gap.csv")
    failed = publish_report(station["id"], DAY)
    assert not failed.ok
    assert db.latest_failure(station["id"], DAY, only_unresolved=True) is not None

    import_observations(station["id"], wide_csv(), "full.csv")
    success = publish_report(station["id"], DAY)
    assert success.ok
    assert db.latest_failure(station["id"], DAY, only_unresolved=True) is None
    # 历史失败记录仍保留可追溯。
    history = db.latest_failure(station["id"], DAY)
    assert history is not None and history["resolved"] == 1


def test_no_report_when_never_published(station):
    import_observations(station["id"], wide_csv(overrides={"CO": {h: None for h in range(24)}}), "day.csv")
    outcome = publish_report(station["id"], DAY)
    assert not outcome.ok
    assert outcome.previous_report is None
    assert db.get_current_report(station["id"], DAY) is None


def test_threshold_snapshot_frozen_after_new_upload(station):
    import_observations(station["id"], wide_csv(), "day.csv")
    first = publish_report(station["id"], DAY)

    # 上传一份把 PM2.5 断点整体放宽的新阈值表，再发布新版本。
    relaxed = DEFAULT_THRESHOLD_CSV.replace("PM2.5,24h,0,35,0,50", "PM2.5,24h,0,50,0,50")
    relaxed = relaxed.replace("PM2.5,24h,35,75,50,100", "PM2.5,24h,50,80,50,100")
    relaxed = relaxed.replace("PM2.5,24h,75,115,100,150", "PM2.5,24h,80,115,100,150")
    from app.services import save_threshold_upload

    saved = save_threshold_upload(relaxed.encode(), "relaxed.csv")
    second = publish_report(station["id"], DAY, threshold_table_id=saved["id"])
    assert second.ok

    old = db.get_report(first.report_id)
    new = db.get_report(second.report_id)
    assert old["threshold_hash"] != new["threshold_hash"]
    assert old["threshold_snapshot"]["pollutants"]["PM2.5"][0]["concentration_high"] == 35.0
    assert new["threshold_snapshot"]["pollutants"]["PM2.5"][0]["concentration_high"] == 50.0
    assert old["readings"]["pollutants"]["PM2.5"]["iaqi"] == 29  # 20 → 0–35 段
    assert new["readings"]["pollutants"]["PM2.5"]["iaqi"] == 20  # 20 → 0–50 段


def test_publish_unknown_station_rejected(db_path):
    with pytest.raises(ServiceError, match="站点不存在"):
        publish_report(999, DAY)


def test_publish_bad_date_rejected(station):
    with pytest.raises(ServiceError, match="日期"):
        publish_report(station["id"], "2026/11/01")


def test_api_full_flow(client):
    r = client.post(
        "/api/stations",
        json={"code": "API01", "name": "接口站", "latitude": 30.5, "longitude": 114.3},
    )
    assert r.status_code == 200, r.text
    station_id = r.json()["station"]["id"]

    r = client.post(
        f"/api/stations/{station_id}/observations",
        files={"file": ("day.csv", wide_csv(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["added"] == 144

    r = client.post("/api/reports", json={"station_id": station_id, "report_date": DAY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["published"] is True
    assert body["version"] == 1
    report_id = body["report_id"]

    r = client.get(f"/api/reports/{report_id}")
    assert r.status_code == 200
    assert r.json()["report"]["report_date"] == DAY

    # 缺测日发布失败：422，且不创建版本。
    r = client.post(
        "/api/reports", json={"station_id": station_id, "report_date": "2026-11-02"}
    )
    assert r.status_code == 422
    failure = r.json()
    assert failure["published"] is False
    assert failure["reasons"]
    assert failure["previous_report_id"] is None


def test_api_threshold_upload_and_snapshot(client):
    csv_text = DEFAULT_THRESHOLD_CSV
    r = client.post(
        "/api/thresholds", files={"file": ("t.csv", csv_text.encode(), "text/csv")}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["row_count"] == 41

    r = client.get(f"/thresholds/{body['threshold_id']}/snapshot.json")
    assert r.status_code == 200
    assert r.json()["content_hash"] == body["content_hash"]


def test_api_rejects_invalid_threshold(client):
    r = client.post(
        "/api/thresholds", files={"file": ("bad.csv", b"pollutant,period\nPM2.5,24h\n", "text/csv")}
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_api_pages_render(client):
    for path in ["/", "/upload", "/thresholds", "/reports", "/demo/manual"]:
        assert client.get(path).status_code == 200, path


def test_api_station_missing_returns_404(client):
    assert client.get("/api/reports/999").status_code == 404
    assert client.get("/stations/999").status_code == 404
