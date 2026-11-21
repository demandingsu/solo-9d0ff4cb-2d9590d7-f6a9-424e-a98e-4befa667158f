"""日报计算引擎：完整性判定、日均值、8 小时滑动均值与 IAQI。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import POLLUTANTS, POLLUTANT_ORDER
from .thresholds import ThresholdTable, ThresholdError
from .utils import round_half_up, utc_now

AQI_LEVELS = (
    (50, "优", "#00a651", "空气质量令人满意，基本无空气污染"),
    (100, "良", "#f2c500", "空气质量可接受，少数敏感人群应减少户外活动"),
    (150, "轻度污染", "#f58220", "敏感人群症状可能加重，建议减少长时间户外活动"),
    (200, "中度污染", "#e60012", "建议减少户外运动，敏感人群避免户外活动"),
    (300, "重度污染", "#8b1a4f", "建议避免户外活动，儿童与老人留在室内"),
    (500, "严重污染", "#6b0f1a", "健康风险很高，应尽量避免外出并关闭门窗"),
)


def aqi_level(aqi: int) -> tuple[str, str, str]:
    for upper, label, color, advice in AQI_LEVELS:
        if aqi <= upper:
            return label, color, advice
    return AQI_LEVELS[-1][1], AQI_LEVELS[-1][2], AQI_LEVELS[-1][3]


@dataclass
class PublishResult:
    ok: bool
    report_date: str
    reasons: list[str]
    pollutants: dict[str, dict]
    primary_pollutants: list[str]
    primary_pollutant: str | None
    max_iaqi: int | None
    level: str | None
    level_color: str | None
    advice: str | None
    readings_snapshot: dict
    completeness: dict

    def snapshot(self) -> dict:
        return {
            "report_date": self.report_date,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "pollutants": self.pollutants,
            "primary_pollutants": self.primary_pollutants,
            "primary_pollutant": self.primary_pollutant,
            "max_iaqi": self.max_iaqi,
            "level": self.level,
            "level_color": self.level_color,
            "advice": self.advice,
            "completeness": self.completeness,
        }


def _day_hours(report_date: str) -> list[str]:
    day = date.fromisoformat(report_date)
    return [f"{day.isoformat()}T{hour:02d}:00:00Z" for hour in range(24)]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _missing_hours(report_date: str, present: dict[str, float]) -> list[str]:
    return [hour for hour in _day_hours(report_date) if hour not in present]


def _rolling_windows(
    report_date: str, present: dict[str, float], window: int = 8
) -> tuple[list[dict], int]:
    """计算日内所有完整滑动窗口均值，返回 (窗口列表, 完整窗口数量)。"""

    hours = _day_hours(report_date)
    windows: list[dict] = []
    for start in range(0, len(hours) - window + 1):
        slice_hours = hours[start : start + window]
        if all(hour in present for hour in slice_hours):
            values = [present[hour] for hour in slice_hours]
            windows.append(
                {
                    "start": slice_hours[0],
                    "end": slice_hours[-1],
                    "mean": _mean(values),
                }
            )
    return windows, len(windows)


def evaluate_day(
    thresholds: ThresholdTable,
    observations: dict[str, dict[str, float]],
    report_date: str,
) -> PublishResult:
    """按站点和 UTC 日评估全部六项污染物，决定能否发布。"""

    day = date.fromisoformat(report_date)
    if day > utc_now().date():
        return PublishResult(
            ok=False,
            report_date=report_date,
            reasons=["日报日期不能晚于当前 UTC 日"],
            pollutants={},
            primary_pollutants=[],
            primary_pollutant=None,
            max_iaqi=None,
            level=None,
            level_color=None,
            advice=None,
            readings_snapshot={},
            completeness={},
        )

    pollutants: dict[str, dict] = {}
    reasons: list[str] = []
    completeness: dict[str, dict] = {}

    for key in POLLUTANT_ORDER:
        spec = POLLUTANTS[key]
        series_map = observations.get(key, {}) or {}
        valid_hours = len(series_map)
        required = spec.required_hours
        detail: dict = {
            "key": key,
            "label": spec.label,
            "name": spec.chinese_name,
            "unit": spec.unit,
            "period": spec.period,
            "method": spec.method,
            "method_label": spec.method_label,
            "valid_hours": valid_hours,
            "required_hours": required,
            "sufficient": False,
            "daily_value": None,
            "iaqi": None,
            "provided": bool(series_map),
            "series": [
                {"hour": hour, "value": series_map[hour]}
                for hour in _day_hours(report_date)
                if hour in series_map
            ],
            "missing_hours": _missing_hours(report_date, series_map),
        }

        try:
            if spec.method == "mean":
                if valid_hours < required:
                    detail["insufficient_reason"] = (
                        f"有效小时 {valid_hours}/24，少于要求的 {required} 小时"
                    )
                else:
                    detail["sufficient"] = True
                    detail["daily_value"] = round_half_up(_mean(list(series_map.values())), 1)
                    detail["iaqi"], detail["capped"] = thresholds.iaqi(
                        key, float(detail["daily_value"])
                    )
                    detail["threshold_basis"] = f"24 小时均值 {detail['daily_value']:g} {spec.unit}"
            elif spec.method == "max_rolling_mean":
                windows, complete = _rolling_windows(report_date, series_map, spec.window_hours)
                detail["complete_windows"] = complete
                detail["window_hours"] = spec.window_hours
                if complete == 0:
                    detail["insufficient_reason"] = (
                        f"没有完整的 {spec.window_hours} 小时滑动窗口（至少需要 1 个）"
                    )
                else:
                    best = max(windows, key=lambda item: (item["mean"], item["start"]))
                    detail["sufficient"] = True
                    detail["daily_value"] = round_half_up(best["mean"], 1)
                    detail["best_window"] = {
                        "start": best["start"],
                        "end": best["end"],
                        "mean": round_half_up(best["mean"], 1),
                        "values": [
                            series_map[hour]
                            for hour in _day_hours(report_date)
                            if best["start"] <= hour <= best["end"]
                        ],
                    }
                    detail["iaqi"], detail["capped"] = thresholds.iaqi(
                        key, float(detail["daily_value"])
                    )
                    detail["threshold_basis"] = (
                        f"连续 {spec.window_hours} 小时最大均值 {detail['daily_value']:g} {spec.unit}"
                    )
            elif spec.method == "max_hourly_iaqi":
                if valid_hours < required:
                    detail["insufficient_reason"] = (
                        f"有效小时 {valid_hours}/24，少于要求的 {required} 小时"
                    )
                else:
                    hourly: list[dict] = []
                    for hour in _day_hours(report_date):
                        if hour not in series_map:
                            continue
                        iaqi_value, capped = thresholds.iaqi(key, series_map[hour])
                        hourly.append(
                            {
                                "hour": hour,
                                "value": series_map[hour],
                                "iaqi": iaqi_value,
                                "capped": capped,
                            }
                        )
                    maximum = max(hourly, key=lambda item: (item["iaqi"], -int(item["hour"][11:13])))
                    detail["sufficient"] = True
                    detail["daily_value"] = maximum["value"]
                    detail["iaqi"] = maximum["iaqi"]
                    detail["capped"] = maximum["capped"]
                    detail["peak_hour"] = maximum["hour"]
                    detail["hourly_iaqi"] = hourly
                    detail["threshold_basis"] = (
                        f"小时分指数最大值 {maximum['iaqi']}（{maximum['hour'][11:13]}:00，"
                        f"{maximum['value']:g} {spec.unit}）"
                    )
            else:  # pragma: no cover - 配置保护
                raise ThresholdError(f"未知计算方法 {spec.method}")
        except ThresholdError as exc:
            detail["sufficient"] = False
            detail["insufficient_reason"] = str(exc)

        if not detail["sufficient"]:
            if not detail.get("insufficient_reason"):
                detail["insufficient_reason"] = "数据不足"
            reasons.append(
                f"{spec.label} 缺测：{detail['insufficient_reason']}"
            )

        completeness[key] = {
            "valid_hours": valid_hours,
            "required_hours": required,
            "sufficient": detail["sufficient"],
            "missing_hours": detail["missing_hours"],
        }
        pollutants[key] = detail

    if reasons:
        return PublishResult(
            ok=False,
            report_date=report_date,
            reasons=reasons,
            pollutants=pollutants,
            primary_pollutants=[],
            primary_pollutant=None,
            max_iaqi=None,
            level=None,
            level_color=None,
            advice=None,
            readings_snapshot=pollutants,
            completeness=completeness,
        )

    max_iaqi = max(int(item["iaqi"]) for item in pollutants.values())
    # 取最大分指数对应的全部污染物；并列时全部列出。
    # 说明：HJ 633 在 AQI ≤ 50 时习惯留空“首要污染物”，但本应用按需求始终标明
    # 最大分指数项，页面会同时提示该读数属于“优”。
    primary = [
        key
        for key in POLLUTANT_ORDER
        if pollutants[key]["iaqi"] is not None and int(pollutants[key]["iaqi"]) == max_iaqi
    ]
    level, color, advice = aqi_level(max_iaqi)

    return PublishResult(
        ok=True,
        report_date=report_date,
        reasons=[],
        pollutants=pollutants,
        primary_pollutants=primary,
        primary_pollutant="、".join(primary) if primary else None,
        max_iaqi=max_iaqi,
        level=level,
        level_color=color,
        advice=advice,
        readings_snapshot=pollutants,
        completeness=completeness,
    )


def readings_for_storage(result: PublishResult) -> dict:
    """精简后的读数快照，写入 reports.readings_json。"""

    return {
        "pollutants": result.pollutants,
        "primary_pollutants": result.primary_pollutants,
        "max_iaqi": result.max_iaqi,
        "level": result.level,
    }
