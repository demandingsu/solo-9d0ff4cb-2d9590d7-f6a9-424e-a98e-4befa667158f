"""共享的 Jinja2 模板环境与过滤器。"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .calc import aqi_level
from .config import APP_NAME, POLLUTANTS, POLLUTANT_ORDER, PERIOD_LABELS
from .utils import fmt_datetime, fmt_number
from .version import __version__

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    env = templates.env
    env.filters["num"] = fmt_number
    env.filters["dt"] = fmt_datetime
    env.globals.update(
        app_name=APP_NAME,
        pollutant_specs=POLLUTANTS,
        pollutant_order=POLLUTANT_ORDER,
        period_labels=PERIOD_LABELS,
        app_version=__version__,
        aqi_level=aqi_level,
    )
    return templates


templates = build_templates()
