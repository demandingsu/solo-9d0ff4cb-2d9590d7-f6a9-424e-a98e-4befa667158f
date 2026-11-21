"""应用配置、污染物定义与默认阈值表。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data"

APP_NAME = "空气质量日报"


@dataclass(frozen=True)
class PollutantSpec:
    key: str
    label: str
    chinese_name: str
    unit: str
    period: str
    method: str
    required_hours: int = 18
    window_hours: int = 8
    order: int = 0

    @property
    def method_label(self) -> str:
        if self.method == "mean":
            return "24 小时算术均值"
        if self.method == "max_rolling_mean":
            return "连续 8 小时滑动均值最大值"
        if self.method == "max_hourly_iaqi":
            return "小时分指数日最大值"
        return self.method


POLLUTANTS: dict[str, PollutantSpec] = {
    "PM2.5": PollutantSpec(
        key="PM2.5",
        label="PM2.5",
        chinese_name="细颗粒物",
        unit="µg/m³",
        period="24h",
        method="mean",
        order=10,
    ),
    "PM10": PollutantSpec(
        key="PM10",
        label="PM10",
        chinese_name="可吸入颗粒物",
        unit="µg/m³",
        period="24h",
        method="mean",
        order=20,
    ),
    "O3": PollutantSpec(
        key="O3",
        label="O₃",
        chinese_name="臭氧",
        unit="µg/m³",
        period="8h",
        method="max_rolling_mean",
        order=30,
    ),
    "SO2": PollutantSpec(
        key="SO2",
        label="SO₂",
        chinese_name="二氧化硫",
        unit="µg/m³",
        period="1h",
        method="max_hourly_iaqi",
        order=40,
    ),
    "NO2": PollutantSpec(
        key="NO2",
        label="NO₂",
        chinese_name="二氧化氮",
        unit="µg/m³",
        period="1h",
        method="max_hourly_iaqi",
        order=50,
    ),
    "CO": PollutantSpec(
        key="CO",
        label="CO",
        chinese_name="一氧化碳",
        unit="mg/m³",
        period="1h",
        method="max_hourly_iaqi",
        order=60,
    ),
}

POLLUTANT_ORDER = [spec.key for spec in sorted(POLLUTANTS.values(), key=lambda s: s.order)]

# 输入别名 -> 规范键
POLLUTANT_ALIASES = {
    "pm2.5": "PM2.5",
    "pm25": "PM2.5",
    "pm-2.5": "PM2.5",
    "细颗粒物": "PM2.5",
    "pm10": "PM10",
    "pm-10": "PM10",
    "可吸入颗粒物": "PM10",
    "o3": "O3",
    "o₃": "O3",
    "臭氧": "O3",
    "so2": "SO2",
    "so₂": "SO2",
    "二氧化硫": "SO2",
    "no2": "NO2",
    "no₂": "NO2",
    "二氧化氮": "NO2",
    "co": "CO",
    "一氧化碳": "CO",
}

PERIOD_ALIASES = {
    "1h": "1h",
    "1h均值": "1h",
    "1小时": "1h",
    "小时": "1h",
    "hourly": "1h",
    "8h": "8h",
    "8h均值": "8h",
    "8小时": "8h",
    "8hour": "8h",
    "24h": "24h",
    "24h均值": "24h",
    "24小时": "24h",
    "24小时均值": "24h",
    "日": "24h",
    "daily": "24h",
}

PERIOD_LABELS = {"1h": "1 小时", "8h": "8 小时", "24h": "24 小时"}


def database_path() -> str:
    return os.environ.get("AIR_DATABASE_PATH", str(DEFAULT_DATA_DIR / "air_quality.db"))


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMPORT_ROWS = 200_000
MAX_ERRORS_RETURNED = 50


DEFAULT_THRESHOLD_CSV = """pollutant,period,concentration_low,concentration_high,index_low,index_high,unit,note
PM2.5,24h,0,35,0,50,µg/m³,一级
PM2.5,24h,35,75,50,100,µg/m³,二级
PM2.5,24h,75,115,100,150,µg/m³,三级
PM2.5,24h,115,150,150,200,µg/m³,四级
PM2.5,24h,150,250,200,300,µg/m³,五级
PM2.5,24h,250,350,300,400,µg/m³,六级
PM2.5,24h,350,500,400,500,µg/m³,上限
PM10,24h,0,50,0,50,µg/m³,一级
PM10,24h,50,150,50,100,µg/m³,二级
PM10,24h,150,250,100,150,µg/m³,三级
PM10,24h,250,350,150,200,µg/m³,四级
PM10,24h,350,420,200,300,µg/m³,五级
PM10,24h,420,500,300,400,µg/m³,六级
PM10,24h,500,600,400,500,µg/m³,上限
O3,8h,0,100,0,50,µg/m³,一级
O3,8h,100,160,50,100,µg/m³,二级
O3,8h,160,215,100,150,µg/m³,三级
O3,8h,215,265,150,200,µg/m³,四级
O3,8h,265,800,200,300,µg/m³,五级
O3,8h,800,1200,300,500,µg/m³,上限扩展
SO2,1h,0,150,0,50,µg/m³,一级
SO2,1h,150,500,50,100,µg/m³,二级
SO2,1h,500,650,100,150,µg/m³,三级
SO2,1h,650,800,150,200,µg/m³,四级
SO2,1h,800,1600,200,300,µg/m³,五级
SO2,1h,1600,2100,300,400,µg/m³,六级
SO2,1h,2100,2620,400,500,µg/m³,上限
NO2,1h,0,200,0,50,µg/m³,一级
NO2,1h,200,400,50,100,µg/m³,二级
NO2,1h,400,700,100,150,µg/m³,三级
NO2,1h,700,1200,150,200,µg/m³,四级
NO2,1h,1200,2340,200,300,µg/m³,五级
NO2,1h,2340,3090,300,400,µg/m³,六级
NO2,1h,3090,3840,400,500,µg/m³,上限
CO,1h,0,5,0,50,mg/m³,一级
CO,1h,5,10,50,100,mg/m³,二级
CO,1h,10,35,100,150,mg/m³,三级
CO,1h,35,60,150,200,mg/m³,四级
CO,1h,60,90,200,300,mg/m³,五级
CO,1h,90,120,300,400,mg/m³,六级
CO,1h,120,150,400,500,mg/m³,上限
"""
