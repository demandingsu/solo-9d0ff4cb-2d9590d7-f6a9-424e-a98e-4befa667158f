"""HTTP 路由：网页与 JSON API。"""

from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import db, services
from .calc import AQI_LEVELS
from .config import DEFAULT_THRESHOLD_CSV, MAX_ERRORS_RETURNED
from .templating import templates
from .services import ServiceError
from .utils import parse_report_date, previous_utc_day, utc_now_iso

router = APIRouter()
api_router = APIRouter()

SAMPLE_OBSERVATIONS = """time,PM2.5,PM10,O3,SO2,NO2,CO
2026-11-19T00:00:00Z,38,72,42,12,38,1.1
2026-11-19T01:00:00Z,41,78,45,11,41,1.2
...
"""


def _ctx(request: Request, **kwargs) -> dict:
    base = {
        "request": request,
        "active": "",
        "error": None,
        "message": None,
        "warnings": [],
        "today": utc_now_iso()[:10],
        "default_date": previous_utc_day(),
    }
    base.update(kwargs)
    return base


def _flash(request: Request) -> tuple[str | None, str | None]:
    return request.query_params.get("msg"), request.query_params.get("err")


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    stations = db.list_stations()
    reports = db.list_reports(limit=12)
    thresholds = db.list_threshold_tables()
    imports = db.list_imports(limit=8)
    message, error = _flash(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            active="dashboard",
            stations=stations,
            reports=reports,
            thresholds=thresholds,
            imports=imports,
            message=message,
            error=error,
        ),
    )


# ------------------------------------------------------------------ 上传页


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    stations = db.list_stations()
    thresholds = db.list_threshold_tables()
    message, error = _flash(request)
    return templates.TemplateResponse(
        request,
        "upload.html",
        _ctx(
            request,
            active="upload",
            stations=stations,
            thresholds=thresholds,
            sample=SAMPLE_OBSERVATIONS,
            message=message,
            error=error,
        ),
    )


@router.post("/upload/station")
def upload_station(
    request: Request,
    code: str = Form(...),
    name: str = Form(""),
    latitude: str = Form(...),
    longitude: str = Form(...),
):
    try:
        station = services.save_station(code, name, latitude, longitude)
    except (ServiceError, ValueError) as exc:
        return RedirectResponse(f"/upload?err={_q(str(exc))}", status_code=303)
    return RedirectResponse(
        f"/stations/{station['id']}?msg={_q('站点已保存')}", status_code=303
    )


@router.post("/upload/observations")
async def upload_observations(
    request: Request,
    station_id: int = Form(...),
    file: UploadFile = File(...),
):
    content = await file.read()
    try:
        outcome = services.import_observations(station_id, content, file.filename or "upload.csv")
    except ServiceError as exc:
        detail = "；".join(exc.errors[:5]) if exc.errors else ""
        message = str(exc) + (f"（示例：{detail}）" if detail else "")
        return RedirectResponse(f"/upload?err={_q(message)}", status_code=303)

    counts = outcome.counts
    summary = (
        f"已导入 {counts['added']} 条、更正 {counts['corrected']} 条，"
        f"未变 {counts.get('unchanged', 0)} 条"
    )
    if counts.get("remapped"):
        summary += f"；其中 {counts['remapped']} 条按站号互换规则归入对站"
    if outcome.errors:
        summary += f"；{len(outcome.errors)} 行缺测/无效"
    return RedirectResponse(
        f"/stations/{station_id}?msg={_q(summary)}&dates={_q(','.join(outcome.dates))}",
        status_code=303,
    )


@router.post("/upload/thresholds")
async def upload_thresholds(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    try:
        saved = services.save_threshold_upload(content, file.filename or "thresholds.csv")
    except ServiceError as exc:
        return RedirectResponse(f"/upload?err={_q(str(exc))}", status_code=303)
    return RedirectResponse(
        f"/thresholds?table_id={saved['id']}&msg={_q('阈值表已保存并冻结为快照')}",
        status_code=303,
    )


@router.get("/thresholds", response_class=HTMLResponse)
def thresholds_page(request: Request, table_id: int | None = None):
    tables = db.list_threshold_tables()
    record = db.get_threshold_table(table_id)
    message, error = _flash(request)
    if record is None:
        # 没有任何阈值表时给出空状态，而不是重定向到自身造成循环。
        return templates.TemplateResponse(
            request,
            "thresholds.html",
            _ctx(
                request,
                active="thresholds",
                tables=tables,
                record=None,
                snapshot=None,
                csv_text="",
                message=message,
                error=error,
            ),
        )
    snapshot = json.loads(record["snapshot_json"])
    message, error = _flash(request)
    return templates.TemplateResponse(
        request,
        "thresholds.html",
        _ctx(
            request,
            active="thresholds",
            tables=tables,
            record=record,
            snapshot=snapshot,
            csv_text=record["csv_text"],
            message=message,
            error=error,
        ),
    )


@router.get("/thresholds/default.csv", response_class=PlainTextResponse)
def download_default_thresholds():
    return PlainTextResponse(
        DEFAULT_THRESHOLD_CSV,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="thresholds_default.csv"'},
    )


@router.get("/thresholds/{table_id}/snapshot.json")
def threshold_snapshot_json(table_id: int):
    record = db.get_threshold_table(table_id)
    if record is None:
        raise HTTPException(status_code=404, detail="阈值表不存在")
    return JSONResponse(json.loads(record["snapshot_json"]))


# ------------------------------------------------------------------ 站点页


@router.get("/stations/{station_id}", response_class=HTMLResponse)
def station_page(
    request: Request,
    station_id: int,
    date: str | None = None,
):
    station = db.get_station(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="站点不存在")
    selected = date or request.query_params.get("dates", "").split(",")[-1] or previous_utc_day()
    try:
        selected = parse_report_date(selected)
    except ValueError:
        selected = previous_utc_day()

    current = db.get_current_report(station_id, selected)
    versions = db.list_report_versions(station_id, selected)
    failure = db.latest_failure(station_id, selected)
    unresolved_failure = db.latest_failure(station_id, selected, only_unresolved=True)
    coverage = db.observation_coverage(station_id, selected)
    imports = db.list_imports(station_id=station_id, limit=12)
    swaps = db.list_swaps_for_station(station_id)
    thresholds = db.list_threshold_tables()
    message, error = _flash(request)
    return templates.TemplateResponse(
        request,
        "station.html",
        _ctx(
            request,
            active="stations",
            station=station,
            selected_date=selected,
            current=current,
            versions=versions,
            failure=failure,
            unresolved_failure=unresolved_failure,
            coverage=coverage,
            imports=imports,
            swaps=swaps,
            thresholds=thresholds,
            message=message,
            error=error,
        ),
    )


@router.post("/stations/{station_id}/delete")
def delete_station_page(station_id: int):
    if not services.delete_station(station_id):
        raise HTTPException(status_code=404, detail="站点不存在")
    return RedirectResponse(f"/?msg={_q('站点及其数据已删除')}", status_code=303)


# --------------------------------------------------------------- 站号互换


@router.get("/swaps", response_class=HTMLResponse)
def swaps_page(request: Request):
    stations = db.list_stations()
    swaps = db.list_swaps()
    message, error = _flash(request)
    return templates.TemplateResponse(
        request,
        "swaps.html",
        _ctx(
            request,
            active="swaps",
            stations=stations,
            swaps=swaps,
            message=message,
            error=error,
        ),
    )


@router.post("/swaps")
def create_swap_form(
    station_a_id: int = Form(...),
    station_b_id: int = Form(...),
    start_at: str = Form(...),
    end_at: str = Form(...),
    note: str = Form(""),
):
    try:
        outcome = services.register_station_swap(
            station_a_id, station_b_id, start_at, end_at, note=note
        )
    except ServiceError as exc:
        return RedirectResponse(f"/swaps?err={_q(str(exc))}", status_code=303)
    swap = outcome["swap"]
    pair_label = f"{swap['station_a_code']} ⇄ {swap['station_b_code']}"
    if not outcome["created"]:
        message = f"{pair_label} 在该时段的互换规则已存在，本次未重复生效"
    else:
        message = (
            f"已登记站号互换 {pair_label}"
            f"（{swap['start_at']} – {swap['end_at']}，含起点不含终点）："
            f"时段内已有读数已互换 {swap['station_a_code']}→{swap['station_b_code']} "
            f"{outcome['moved_a_to_b']} 条、"
            f"{swap['station_b_code']}→{swap['station_a_code']} {outcome['moved_b_to_a']} 条；"
            f"此后按原站号补传/重传的时段内读数将自动归到对站"
        )
    return RedirectResponse(f"/swaps?msg={_q(message)}", status_code=303)


# --------------------------------------------------------------- 发布/查询


@router.post("/publish")
def publish_form(
    station_id: int = Form(...),
    report_date: str = Form(...),
    threshold_table_id: int | None = Form(None),
    note: str = Form(""),
):
    try:
        outcome = services.publish_report(
            station_id, report_date, threshold_table_id=threshold_table_id, note=note
        )
    except ServiceError as exc:
        return RedirectResponse(
            f"/stations/{station_id}?date={report_date}&err={_q(str(exc))}", status_code=303
        )
    if outcome.ok:
        message = f"已发布 v{outcome.version}"
        return RedirectResponse(
            f"/reports/{outcome.report_id}?msg={_q(message)}", status_code=303
        )
    reasons = "；".join(outcome.result.reasons[:4])
    return RedirectResponse(
        f"/stations/{station_id}?date={report_date}&err={_q('发布被拒绝：' + reasons)}",
        status_code=303,
    )


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    station: str | None = None,
    date: str | None = None,
):
    station = (station or "").strip() or None
    if date:
        try:
            date = parse_report_date(date)
        except ValueError:
            date = None
    reports = db.list_reports(station_code=station, report_date=date, limit=200)
    stations = db.list_stations()
    message, error = _flash(request)
    return templates.TemplateResponse(
        request,
        "reports.html",
        _ctx(
            request,
            active="reports",
            reports=reports,
            stations=stations,
            filter_station=station or "",
            filter_date=date or "",
            message=message,
            error=error,
        ),
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_page(request: Request, report_id: int):
    report = db.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="日报不存在")
    versions = db.list_report_versions(report["station_id"], report["report_date"])
    failure = db.latest_failure(report["station_id"], report["report_date"])
    unresolved_failure = db.latest_failure(
        report["station_id"], report["report_date"], only_unresolved=True
    )
    message, error = _flash(request)
    return templates.TemplateResponse(
        request,
        "report.html",
        _ctx(
            request,
            active="reports",
            report=report,
            versions=versions,
            failure=failure,
            unresolved_failure=unresolved_failure,
            message=message,
            error=error,
        ),
    )


@router.get("/reports/{report_id}/readings.json")
def report_readings_json(report_id: int):
    report = db.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="日报不存在")
    return JSONResponse(
        {
            "report_id": report["id"],
            "station": report["station_code"],
            "report_date": report["report_date"],
            "version": report["version"],
            "is_current": bool(report["is_current"]),
            "threshold_hash": report["threshold_hash"],
            "readings": report["readings"],
            "threshold_snapshot": report["threshold_snapshot"],
            "created_at": report["created_at"],
        }
    )


@router.post("/demo")
def run_demo():
    services.create_demo_scenario()
    return RedirectResponse(f"/?msg={_q('演示数据已生成')}", status_code=303)


@router.get("/demo/manual", response_class=HTMLResponse)
def demo_manual(request: Request):
    return templates.TemplateResponse(
        request,
        "demo.html",
        _ctx(request, active="demo", aqi_levels=AQI_LEVELS),
    )


# ------------------------------------------------------------------- API


def _api_error(exc: Exception, status: int = 400) -> JSONResponse:
    errors = getattr(exc, "errors", [])
    return JSONResponse(
        {"ok": False, "detail": str(exc), "errors": errors[:MAX_ERRORS_RETURNED]},
        status_code=status,
    )


@api_router.get("/stations")
def api_list_stations():
    return {"ok": True, "stations": db.list_stations()}


@api_router.post("/stations")
async def api_create_station(request: Request):
    payload = await _json_body(request)
    try:
        station = services.save_station(
            payload.get("code", ""),
            payload.get("name", ""),
            payload.get("latitude"),
            payload.get("longitude"),
        )
    except ServiceError as exc:
        return _api_error(exc)
    return {"ok": True, "station": station}


@api_router.post("/stations/{station_id}/observations")
async def api_import_observations(station_id: int, file: UploadFile = File(...)):
    content = await file.read()
    try:
        outcome = services.import_observations(station_id, content, file.filename or "upload.csv")
    except ServiceError as exc:
        return _api_error(exc)
    return {
        "ok": True,
        "import_id": outcome.import_id,
        "counts": outcome.counts,
        "dates": outcome.dates,
        "warnings": outcome.warnings,
        "errors": outcome.errors[:MAX_ERRORS_RETURNED],
    }


@api_router.get("/thresholds")
def api_list_thresholds():
    return {"ok": True, "thresholds": db.list_threshold_tables()}


@api_router.post("/thresholds")
async def api_upload_thresholds(file: UploadFile = File(...)):
    content = await file.read()
    try:
        saved = services.save_threshold_upload(content, file.filename or "thresholds.csv")
    except ServiceError as exc:
        return _api_error(exc)
    return {
        "ok": True,
        "threshold_id": saved["id"],
        "content_hash": saved["table"].content_hash,
        "row_count": len(saved["table"].ordered_rows()),
    }


@api_router.post("/reports")
async def api_publish(request: Request):
    payload = await _json_body(request)
    try:
        outcome = services.publish_report(
            int(payload.get("station_id")),
            parse_report_date(str(payload.get("report_date", ""))),
            threshold_table_id=payload.get("threshold_table_id"),
            note=payload.get("note", "") or "",
        )
    except (ServiceError, TypeError, ValueError) as exc:
        return _api_error(exc)
    if not outcome.ok:
        body = {
            "ok": False,
            "published": False,
            "report_date": outcome.result.report_date,
            "reasons": outcome.result.reasons,
            "completeness": outcome.result.completeness,
            "previous_report_id": outcome.previous_report["id"] if outcome.previous_report else None,
        }
        return JSONResponse(body, status_code=422)
    return {
        "ok": True,
        "published": True,
        "report_id": outcome.report_id,
        "version": outcome.version,
        "max_iaqi": outcome.result.max_iaqi,
        "level": outcome.result.level,
        "primary_pollutant": outcome.result.primary_pollutant,
        "previous_report_id": outcome.previous_report["id"] if outcome.previous_report else None,
    }


@api_router.get("/swaps")
def api_list_swaps():
    return {"ok": True, "swaps": db.list_swaps()}


@api_router.post("/swaps")
async def api_create_swap(request: Request):
    payload = await _json_body(request)
    try:
        outcome = services.register_station_swap(
            payload.get("station_a_id"),
            payload.get("station_b_id"),
            str(payload.get("start_at", "")),
            str(payload.get("end_at", "")),
            note=payload.get("note", "") or "",
        )
    except services.SwapOverlapError as exc:
        return _api_error(exc, status=409)
    except ServiceError as exc:
        return _api_error(exc)
    return {
        "ok": True,
        "created": outcome["created"],
        "swap": outcome["swap"],
        "moved_a_to_b": outcome["moved_a_to_b"],
        "moved_b_to_a": outcome["moved_b_to_a"],
    }


@api_router.get("/reports")
def api_list_reports(
    station: str | None = None,
    report_date: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    return {
        "ok": True,
        "reports": db.list_reports(station_code=station, report_date=report_date, limit=limit),
    }


@api_router.get("/reports/{report_id}")
def api_get_report(report_id: int):
    report = db.get_report(report_id)
    if report is None:
        return JSONResponse({"ok": False, "detail": "日报不存在"}, status_code=404)
    return {"ok": True, "report": report}


@api_router.get("/reports/{report_id}/versions")
def api_report_versions(report_id: int):
    report = db.get_report(report_id)
    if report is None:
        return JSONResponse({"ok": False, "detail": "日报不存在"}, status_code=404)
    return {
        "ok": True,
        "versions": db.list_report_versions(report["station_id"], report["report_date"]),
    }


@api_router.post("/demo")
def api_demo():
    return {"ok": True, **services.create_demo_scenario()}


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:  # pragma: no cover - 依赖客户端错误格式
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return payload


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
