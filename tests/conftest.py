"""共享测试夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("AIR_DATABASE_PATH", str(path))
    from app import db

    db.init_db()
    yield str(path)


@pytest.fixture()
def client(db_path):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def build_csv(
    day: str,
    values: dict[str, dict[int, float | None]],
    levels: dict[str, float] | None = None,
    include_station: bool = False,
) -> bytes:
    """按 {污染物: {小时: 值}} 生成宽表 CSV。值为 None 表示缺测。"""

    levels = levels or {}
    pollutants = ["PM2.5", "PM10", "O3", "SO2", "NO2", "CO"]
    header = ["time", *pollutants]
    if include_station:
        header = ["station_code", *header]
    rows = [",".join(header)]
    for hour in range(24):
        cells = []
        for pollutant in pollutants:
            value = values.get(pollutant, {}).get(hour, levels.get(pollutant, 10))
            if value is None:
                cells.append("")
            else:
                cells.append(str(value))
        prefix = ["TEST01"] if include_station else []
        rows.append(",".join([f"{day}T{hour:02d}:00:00Z", *prefix, *cells]))
    return ("\n".join(rows) + "\n").encode("utf-8")


def full_day(level: float = 20.0, day: str = "2026-11-01") -> dict[str, dict[int, float]]:
    return {key: {hour: level for hour in range(24)} for key in ["PM2.5", "PM10", "O3", "SO2", "NO2", "CO"]}
