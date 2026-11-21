"""SQLite 数据访问层。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DEFAULT_THRESHOLD_CSV, database_path
from .utils import utc_now_iso

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threshold_tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    csv_text TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    filename TEXT,
    sha256 TEXT,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_valid INTEGER NOT NULL DEFAULT 0,
    rows_missing INTEGER NOT NULL DEFAULT 0,
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_corrected INTEGER NOT NULL DEFAULT 0,
    rows_remapped INTEGER NOT NULL DEFAULT 0,
    dates_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    pollutant TEXT NOT NULL,
    value REAL NOT NULL,
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(station_id, observed_at, pollutant)
);

CREATE INDEX IF NOT EXISTS idx_observations_lookup
    ON observations(station_id, observed_at);

CREATE TABLE IF NOT EXISTS observation_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    pollutant TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    import_id INTEGER,
    revised_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    report_date TEXT NOT NULL,
    version INTEGER NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    supersedes_id INTEGER REFERENCES reports(id) ON DELETE SET NULL,
    threshold_table_id INTEGER REFERENCES threshold_tables(id) ON DELETE SET NULL,
    threshold_hash TEXT NOT NULL,
    threshold_snapshot_json TEXT NOT NULL,
    readings_json TEXT NOT NULL,
    primary_pollutant TEXT,
    max_iaqi INTEGER,
    aqi_level TEXT,
    completeness_json TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(station_id, report_date, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_current
    ON reports(station_id, report_date) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS publish_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    report_date TEXT NOT NULL,
    threshold_table_id INTEGER,
    threshold_hash TEXT,
    reasons_json TEXT NOT NULL,
    readings_json TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 站号互换规则：网关误标时段内，station_a_id 与 station_b_id 的读数互相归属。
-- 站对按较小 id 在前规范化存储；时段为 UTC 整点半开区间 [start_at, end_at)。
CREATE TABLE IF NOT EXISTS station_swaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_a_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    station_b_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    moved_a_to_b INTEGER NOT NULL DEFAULT 0,
    moved_b_to_a INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 同一对站点同一时段只允许登记一次（重复登记幂等返回既有规则）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_station_swaps_unique
    ON station_swaps(station_a_id, station_b_id, start_at, end_at);
"""


def _connect() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db(seed_default_threshold: bool = True) -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    if seed_default_threshold:
        ensure_default_threshold()


def _migrate(conn: sqlite3.Connection) -> None:
    """为已有数据库补齐后续新增列，保持升级无需手工迁移。"""

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(publish_failures)").fetchall()
    }
    if "resolved" not in columns:
        conn.execute(
            "ALTER TABLE publish_failures ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0"
        )

    import_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(imports)").fetchall()
    }
    if "rows_remapped" not in import_columns:
        conn.execute(
            "ALTER TABLE imports ADD COLUMN rows_remapped INTEGER NOT NULL DEFAULT 0"
        )


def ensure_default_threshold() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM threshold_tables ORDER BY id LIMIT 1").fetchone()
        if row:
            return int(row["id"])
    from .thresholds import parse_threshold_csv

    table = parse_threshold_csv(DEFAULT_THRESHOLD_CSV, source="系统内置默认阈值")
    with get_conn() as conn:
        return int(
            conn.execute(
                "INSERT INTO threshold_tables (name, content_hash, csv_text, snapshot_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    "默认阈值（GB 3095 / HJ 633 近似）",
                    table.content_hash,
                    DEFAULT_THRESHOLD_CSV,
                    json.dumps(table.snapshot(), ensure_ascii=False),
                    utc_now_iso(),
                ),
            ).lastrowid
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- stations


def upsert_station(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    latitude: float,
    longitude: float,
) -> int:
    now = utc_now_iso()
    existing = conn.execute("SELECT id FROM stations WHERE code = ?", (code,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE stations SET name = ?, latitude = ?, longitude = ?, updated_at = ? WHERE id = ?",
            (name, latitude, longitude, now, existing["id"]),
        )
        return int(existing["id"])
    return int(
        conn.execute(
            "INSERT INTO stations (code, name, latitude, longitude, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (code, name, latitude, longitude, now, now),
        ).lastrowid
    )


def get_station(station_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        )


def get_station_by_code(code: str) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM stations WHERE code = ?", (code,)).fetchone()
        )


def list_stations() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.*,"
            " (SELECT COUNT(*) FROM observations o WHERE o.station_id = s.id) AS observation_count,"
            " (SELECT MAX(observed_at) FROM observations o WHERE o.station_id = s.id) AS latest_observation,"
            " (SELECT COUNT(*) FROM reports r WHERE r.station_id = s.id) AS report_count,"
            " (SELECT MAX(r.report_date) FROM reports r"
            "   WHERE r.station_id = s.id AND r.is_current = 1) AS latest_report_date"
            " FROM stations s ORDER BY s.code"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------- observations


def upsert_observation(
    conn: sqlite3.Connection,
    station_id: int,
    observed_at: str,
    pollutant: str,
    value: float,
    import_id: int,
) -> str:
    """写入或更正观测值，返回 added / unchanged / corrected。"""

    now = utc_now_iso()
    existing = conn.execute(
        "SELECT id, value FROM observations WHERE station_id = ? AND observed_at = ? AND pollutant = ?",
        (station_id, observed_at, pollutant),
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO observations (station_id, observed_at, pollutant, value, import_id, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (station_id, observed_at, pollutant, value, import_id, now),
        )
        return "added"
    if abs(float(existing["value"]) - value) < 1e-12:
        conn.execute(
            "UPDATE observations SET import_id = ?, updated_at = ? WHERE id = ?",
            (import_id, now, existing["id"]),
        )
        return "unchanged"
    conn.execute(
        "UPDATE observations SET value = ?, import_id = ?, updated_at = ? WHERE id = ?",
        (value, import_id, now, existing["id"]),
    )
    conn.execute(
        "INSERT INTO observation_revisions"
        " (station_id, observed_at, pollutant, old_value, new_value, import_id, revised_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (station_id, observed_at, pollutant, existing["value"], value, import_id, now),
    )
    return "corrected"


def observations_for_day(station_id: int, report_date: str) -> dict[str, dict[str, float]]:
    """
    返回 {pollutant: {observed_at: value}}，仅含该 UTC 日 [00:00, 24:00)。
    """

    from .utils import day_bounds

    start, end = day_bounds(report_date)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pollutant, observed_at, value FROM observations"
            " WHERE station_id = ? AND observed_at >= ? AND observed_at < ?"
            " ORDER BY observed_at",
            (station_id, start, end),
        ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        result.setdefault(row["pollutant"], {})[row["observed_at"]] = float(row["value"])
    return result


def observation_coverage(station_id: int, report_date: str) -> dict[str, dict]:
    """返回每项的 {count: 有效小时数, hours: {小时序号: 数值}, windows: 完整 8h 窗口数}。"""

    data = observations_for_day(station_id, report_date)
    coverage: dict[str, dict] = {}
    for pollutant, values in data.items():
        present_hours = {int(hour[11:13]) for hour in values}
        complete_windows = sum(
            1 for start in range(24 - 8 + 1) if set(range(start, start + 8)) <= present_hours
        )
        coverage[pollutant] = {
            "count": len(values),
            "hours": {int(hour[11:13]): value for hour, value in values.items()},
            "windows": complete_windows,
        }
    return coverage


# -------------------------------------------------------------- thresholds


def save_threshold_table(
    conn: sqlite3.Connection, name: str, csv_text: str, snapshot: dict
) -> int:
    return int(
        conn.execute(
            "INSERT INTO threshold_tables (name, content_hash, csv_text, snapshot_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                name,
                snapshot.get("content_hash", ""),
                csv_text,
                json.dumps(snapshot, ensure_ascii=False),
                utc_now_iso(),
            ),
        ).lastrowid
    )


def list_threshold_tables() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, content_hash, created_at FROM threshold_tables ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_threshold_table(table_id: int | None = None) -> dict | None:
    with get_conn() as conn:
        if table_id is None:
            row = conn.execute(
                "SELECT * FROM threshold_tables ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM threshold_tables WHERE id = ?", (table_id,)
            ).fetchone()
    return row_to_dict(row)


# ----------------------------------------------------------------- imports


def record_import(
    conn: sqlite3.Connection,
    station_id: int,
    filename: str,
    sha256: str,
    counts: dict[str, int],
    dates: list[str],
) -> int:
    return int(
        conn.execute(
            "INSERT INTO imports (station_id, filename, sha256, rows_total, rows_valid,"
            " rows_missing, rows_added, rows_corrected, rows_remapped, dates_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                station_id,
                filename,
                sha256,
                counts.get("total", 0),
                counts.get("valid", 0),
                counts.get("missing", 0),
                counts.get("added", 0),
                counts.get("corrected", 0),
                counts.get("remapped", 0),
                json.dumps(dates),
                utc_now_iso(),
            ),
        ).lastrowid
    )


def list_imports(limit: int = 20, station_id: int | None = None) -> list[dict]:
    query = (
        "SELECT i.*, s.code AS station_code, s.name AS station_name"
        " FROM imports i JOIN stations s ON s.id = i.station_id"
    )
    params: list[Any] = []
    if station_id is not None:
        query += " WHERE i.station_id = ?"
        params.append(station_id)
    query += " ORDER BY i.id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["dates"] = json.loads(item.pop("dates_json") or "[]")
        result.append(item)
    return result


# ----------------------------------------------------------------- reports


def next_version(conn: sqlite3.Connection, station_id: int, report_date: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM reports WHERE station_id = ? AND report_date = ?",
        (station_id, report_date),
    ).fetchone()
    return int(row["v"]) + 1


def create_report(
    conn: sqlite3.Connection,
    station_id: int,
    report_date: str,
    threshold_table_id: int | None,
    threshold_hash: str,
    threshold_snapshot: dict,
    readings: dict,
    primary_pollutant: str,
    max_iaqi: int,
    aqi_level: str,
    completeness: dict,
    note: str = "",
) -> int:
    version = next_version(conn, station_id, report_date)
    previous = conn.execute(
        "SELECT id FROM reports WHERE station_id = ? AND report_date = ? AND is_current = 1",
        (station_id, report_date),
    ).fetchone()
    previous_id = int(previous["id"]) if previous else None
    conn.execute(
        "UPDATE reports SET is_current = 0 WHERE station_id = ? AND report_date = ? AND is_current = 1",
        (station_id, report_date),
    )
    report_id = int(
        conn.execute(
            "INSERT INTO reports (station_id, report_date, version, is_current, supersedes_id,"
            " threshold_table_id, threshold_hash, threshold_snapshot_json, readings_json,"
            " primary_pollutant, max_iaqi, aqi_level, completeness_json, note, created_at)"
            " VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
            (
                station_id,
                report_date,
                version,
                previous_id,
                threshold_table_id,
                threshold_hash,
                json.dumps(threshold_snapshot, ensure_ascii=False),
                json.dumps(readings, ensure_ascii=False),
                primary_pollutant,
                max_iaqi,
                aqi_level,
                json.dumps(completeness, ensure_ascii=False),
                note,
                utc_now_iso(),
            ),
        ).lastrowid
    )
    return report_id


def record_publish_failure(
    conn: sqlite3.Connection,
    station_id: int,
    report_date: str,
    threshold_table_id: int | None,
    threshold_hash: str,
    reasons: list[str],
    readings: dict,
) -> int:
    return int(
        conn.execute(
            "INSERT INTO publish_failures (station_id, report_date, threshold_table_id,"
            " threshold_hash, reasons_json, readings_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                station_id,
                report_date,
                threshold_table_id,
                threshold_hash,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(readings, ensure_ascii=False),
                utc_now_iso(),
            ),
        ).lastrowid
    )


def _report_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["threshold_snapshot"] = json.loads(item.pop("threshold_snapshot_json"))
    item["readings"] = json.loads(item.pop("readings_json"))
    item["completeness"] = json.loads(item.pop("completeness_json"))
    return item


def get_report(report_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.*, s.code AS station_code, s.name AS station_name,"
            " s.latitude AS latitude, s.longitude AS longitude"
            " FROM reports r JOIN stations s ON s.id = r.station_id WHERE r.id = ?",
            (report_id,),
        ).fetchone()
    return _report_row(row) if row else None


def get_current_report(station_id: int, report_date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.*, s.code AS station_code, s.name AS station_name,"
            " s.latitude AS latitude, s.longitude AS longitude"
            " FROM reports r JOIN stations s ON s.id = r.station_id"
            " WHERE r.station_id = ? AND r.report_date = ? AND r.is_current = 1",
            (station_id, report_date),
        ).fetchone()
    return _report_row(row) if row else None


def list_report_versions(station_id: int, report_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, version, is_current, max_iaqi, primary_pollutant, aqi_level,"
            " threshold_hash, created_at, supersedes_id"
            " FROM reports WHERE station_id = ? AND report_date = ? ORDER BY version DESC",
            (station_id, report_date),
        ).fetchall()
    return [dict(row) for row in rows]


def list_reports(
    station_code: str | None = None,
    report_date: str | None = None,
    limit: int = 100,
) -> list[dict]:
    query = (
        "SELECT r.id, r.station_id, r.report_date, r.version, r.is_current, r.max_iaqi, r.primary_pollutant,"
        " r.aqi_level, r.created_at, s.code AS station_code, s.name AS station_name"
        " FROM reports r JOIN stations s ON s.id = r.station_id WHERE 1=1"
    )
    params: list[Any] = []
    if station_code:
        query += " AND s.code = ?"
        params.append(station_code)
    if report_date:
        query += " AND r.report_date = ?"
        params.append(report_date)
    query += " ORDER BY r.report_date DESC, s.code, r.version DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def resolve_failures(conn: sqlite3.Connection, station_id: int, report_date: str) -> None:
    """成功发布后把同站同日的失败记录标记为已解决。"""

    conn.execute(
        "UPDATE publish_failures SET resolved = 1 WHERE station_id = ? AND report_date = ?",
        (station_id, report_date),
    )


def latest_failure(
    station_id: int, report_date: str, only_unresolved: bool = False
) -> dict | None:
    query = (
        "SELECT * FROM publish_failures WHERE station_id = ? AND report_date = ?"
    )
    if only_unresolved:
        query += " AND resolved = 0"
    query += " ORDER BY id DESC LIMIT 1"
    with get_conn() as conn:
        row = conn.execute(query, (station_id, report_date)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["reasons"] = json.loads(item.pop("reasons_json"))
    item["readings"] = json.loads(item.pop("readings_json") or "{}")
    return item


def latest_report_for_station(station_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.*, s.code AS station_code, s.name AS station_name,"
            " s.latitude AS latitude, s.longitude AS longitude"
            " FROM reports r JOIN stations s ON s.id = r.station_id"
            " WHERE r.station_id = ? AND r.is_current = 1"
            " ORDER BY r.report_date DESC LIMIT 1",
            (station_id,),
        ).fetchone()
    return _report_row(row) if row else None


# ---------------------------------------------------------------- 站号互换


def insert_swap(
    conn: sqlite3.Connection,
    station_a_id: int,
    station_b_id: int,
    start_at: str,
    end_at: str,
    note: str,
) -> int:
    return int(
        conn.execute(
            "INSERT INTO station_swaps"
            " (station_a_id, station_b_id, start_at, end_at, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (station_a_id, station_b_id, start_at, end_at, note, utc_now_iso()),
        ).lastrowid
    )


def update_swap_counts(
    conn: sqlite3.Connection, swap_id: int, moved_a_to_b: int, moved_b_to_a: int
) -> None:
    conn.execute(
        "UPDATE station_swaps SET moved_a_to_b = ?, moved_b_to_a = ? WHERE id = ?",
        (moved_a_to_b, moved_b_to_a, swap_id),
    )


def find_swap(
    conn: sqlite3.Connection,
    station_a_id: int,
    station_b_id: int,
    start_at: str,
    end_at: str,
) -> dict | None:
    """按规范化站对 + 时段精确查找既有规则（用于幂等登记）。"""

    return row_to_dict(
        conn.execute(
            "SELECT * FROM station_swaps"
            " WHERE station_a_id = ? AND station_b_id = ? AND start_at = ? AND end_at = ?",
            (station_a_id, station_b_id, start_at, end_at),
        ).fetchone()
    )


def fetch_swaps(conn: sqlite3.Connection) -> list[dict]:
    """取出全部互换规则。导入时在事务内调用一次，保证整批使用同一套已提交规则。"""

    rows = conn.execute("SELECT * FROM station_swaps ORDER BY start_at, id").fetchall()
    return [dict(row) for row in rows]


_SWAP_SELECT = (
    "SELECT sw.*, sa.code AS station_a_code, sa.name AS station_a_name,"
    " sb.code AS station_b_code, sb.name AS station_b_name"
    " FROM station_swaps sw"
    " JOIN stations sa ON sa.id = sw.station_a_id"
    " JOIN stations sb ON sb.id = sw.station_b_id"
)


def get_swap(swap_id: int) -> dict | None:
    with get_conn() as conn:
        return row_to_dict(
            conn.execute(_SWAP_SELECT + " WHERE sw.id = ?", (swap_id,)).fetchone()
        )


def list_swaps() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            _SWAP_SELECT + " ORDER BY sw.start_at DESC, sw.id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def list_swaps_for_station(station_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            _SWAP_SELECT
            + " WHERE sw.station_a_id = ? OR sw.station_b_id = ?"
            " ORDER BY sw.start_at DESC, sw.id DESC",
            (station_id, station_id),
        ).fetchall()
    return [dict(row) for row in rows]


def swap_observations(
    conn: sqlite3.Connection,
    station_a_id: int,
    station_b_id: int,
    start_at: str,
    end_at: str,
) -> tuple[int, int]:
    """把 [start_at, end_at) 内两站的已有观测互换归属，返回 (A→B 条数, B→A 条数)。

    先整批取出并删除，再按对站重新插入：
    - 保留各自的导入批次号（import_id 不变）；
    - 两站同一小时同一污染物都存在时也不会触发唯一约束的瞬态冲突；
    - 与规则登记在同一事务内执行，失败时整体回滚，读数归属保持不变。
    """

    def _collect(station_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT id, observed_at, pollutant, value, import_id FROM observations"
            " WHERE station_id = ? AND observed_at >= ? AND observed_at < ?",
            (station_id, start_at, end_at),
        ).fetchall()

    rows_a = _collect(station_a_id)
    rows_b = _collect(station_b_id)
    if not rows_a and not rows_b:
        return 0, 0
    ids = [int(row["id"]) for row in (*rows_a, *rows_b)]
    conn.execute(
        f"DELETE FROM observations WHERE id IN ({','.join('?' for _ in ids)})",
        ids,
    )
    now = utc_now_iso()
    moved = [
        *(
            (station_b_id, row["observed_at"], row["pollutant"], row["value"], row["import_id"], now)
            for row in rows_a
        ),
        *(
            (station_a_id, row["observed_at"], row["pollutant"], row["value"], row["import_id"], now)
            for row in rows_b
        ),
    ]
    conn.executemany(
        "INSERT INTO observations (station_id, observed_at, pollutant, value, import_id, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        moved,
    )
    return len(rows_a), len(rows_b)
