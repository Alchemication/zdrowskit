"""Chart extraction and rendering from LLM responses.

The LLM can include ``<chart>`` blocks in its output containing plotly
code.  This module extracts those blocks, executes the code in a restricted
namespace, and returns PNG bytes suitable for Telegram delivery.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CHART_PATTERN = re.compile(
    r'<chart(?:\s+title="([^"]+)")?(?:\s+section="([^"]*)")?\s*>'
    r"(.*?)"
    r"</chart>",
    re.DOTALL,
)

# Timeout in seconds for chart code execution.
_EXEC_TIMEOUT = 10
_PREFERRED_X_KEYS = ("week", "date", "month", "label")
_PREFERRED_Y_KEYS = (
    "avg_pace",
    "avg_pace_min_km",
    "avg_pace_minkm",
    "avg_pace_min_per_km",
    "pace_min_per_km",
    "pace_min_km",
    "pace_minkm",
    "total_km",
    "total_distance_km",
    "distance_km",
    "value",
)
_LOW_VALUE_IS_BETTER_KEYS = ("pace", "rhr", "resting_hr")


@dataclass
class ChartBlock:
    """A single chart extracted from an LLM response."""

    title: str
    section: str
    code: str


@dataclass
class ChartResult:
    """A rendered chart ready for Telegram delivery."""

    title: str
    section: str
    image_bytes: bytes


def chart_figure_caption(index: int, title: str) -> str:
    """Build a stable Telegram caption for a chart attachment.

    Args:
        index: 1-based chart index in the current response.
        title: Human-readable chart title.

    Returns:
        Markdown caption text such as ``"**Figure 1. Pace Trend**"``.
    """
    label = f"Figure {index}"
    clean_title = title.strip()
    if clean_title:
        return f"**{label}. {clean_title}**"
    return f"**{label}**"


def extract_charts(response: str) -> list[ChartBlock]:
    """Extract all ``<chart>`` blocks from the LLM response.

    Args:
        response: Full LLM response text.

    Returns:
        List of :class:`ChartBlock` instances (may be empty).
    """
    blocks: list[ChartBlock] = []
    for match in _CHART_PATTERN.finditer(response):
        title = (match.group(1) or "").strip()
        section = (match.group(2) or "").strip()
        code = match.group(3).strip()
        if code:
            blocks.append(ChartBlock(title=title, section=section, code=code))
    return blocks


def strip_charts(response: str) -> str:
    """Remove all ``<chart>`` blocks from the response text.

    Args:
        response: Full LLM response text.

    Returns:
        The response with chart blocks stripped and excess whitespace cleaned.
    """
    return re.sub(
        r"\s*<chart\s[^>]*>.*?</chart>\s*", "\n", response, flags=re.DOTALL
    ).strip()


def _png_from_figure(fig: object) -> bytes:
    """Render a plotly figure to PNG bytes."""
    return fig.to_image(format="png", width=900, height=450, scale=2)


def _row_chart_keys(rows: list[dict]) -> tuple[str, str] | None:
    """Choose x/y fields for a generic rows chart."""
    if not rows:
        return None
    sample = rows[0]
    x_key = next((key for key in _PREFERRED_X_KEYS if key in sample), None)
    if x_key is None:
        x_key = next(
            (key for key, value in sample.items() if isinstance(value, str)), None
        )
    if x_key is None:
        return None

    y_key = next(
        (
            key
            for key in _PREFERRED_Y_KEYS
            if key in sample and isinstance(sample[key], int | float)
        ),
        None,
    )
    if y_key is None:
        y_key = next(
            (
                key
                for key, value in sample.items()
                if key != x_key
                and isinstance(value, int | float)
                and key not in {"runs", "count"}
            ),
            None,
        )
    if y_key is None:
        return None
    return x_key, y_key


def render_rows_chart(rows: list[dict], title: str = "") -> bytes | None:
    """Render a generic trend chart from query rows.

    Args:
        rows: Query result rows, usually from ``run_sql``.
        title: Optional chart title.

    Returns:
        PNG image bytes, or ``None`` when rows are not chartable.
    """
    keys = _row_chart_keys(rows)
    if keys is None:
        return None
    x_key, y_key = keys

    try:
        import plotly.graph_objects as go

        from config import CHART_THEME

        x = [row[x_key] for row in rows]
        y = [row[y_key] for row in rows]
        fig = go.Figure(
            go.Scatter(
                x=x,
                y=y,
                mode="lines+markers",
                marker=dict(size=10, color="#3498db"),
                line=dict(color="#3498db", width=2),
                name=y_key.replace("_", " "),
            )
        )
        fig.update_layout(
            template=CHART_THEME,
            title=title or y_key.replace("_", " ").title(),
            xaxis_title="",
            yaxis_title=y_key.replace("_", " "),
            margin=dict(l=50, r=30, t=50, b=40),
        )
        if any(marker in y_key for marker in _LOW_VALUE_IS_BETTER_KEYS):
            fig.update_yaxes(autorange="reversed")
        return _png_from_figure(fig)
    except Exception as exc:
        logger.warning("Rows chart fallback failed: %s", exc)
        return None


def render_chart(
    code: str,
    health_data: dict,
    extra_namespace: dict | None = None,
) -> bytes | None:
    """Execute plotly code and return PNG bytes.

    The code is executed in a restricted namespace with ``go``
    (plotly.graph_objects), ``px`` (plotly.express), ``np`` (numpy), and
    ``data`` (the health-data dict) available.  The code must produce a
    ``fig`` variable containing a plotly Figure.

    Args:
        code: Python source code that builds a plotly figure.
        health_data: The health-data dict (same structure the LLM sees).
        extra_namespace: Additional variables to inject into the execution
            namespace (e.g. ``{"rows": [...]}`` from query tool results).

    Returns:
        PNG image bytes, or ``None`` if rendering fails for any reason.
    """
    import numpy as np
    import plotly.express as px
    import plotly.graph_objects as go

    # Build a restricted builtins dict.
    safe_builtins = {
        k: v
        for k, v in __builtins__.items()  # type: ignore[union-attr]
        if k not in {"open", "exec", "eval", "compile", "breakpoint"}
    }

    namespace: dict = {
        "__builtins__": safe_builtins,
        "go": go,
        "data": health_data,
        "px": px,
        "np": np,
    }
    if extra_namespace:
        namespace.update(extra_namespace)

    result_holder: list[bytes | None] = [None]
    error_holder: list[Exception | None] = [None]

    def _run() -> None:
        try:
            exec(code, namespace)  # noqa: S102
            fig = namespace.get("fig")
            if fig is None:
                logger.warning("Chart code did not produce a 'fig' variable")
                return
            result_holder[0] = _png_from_figure(fig)
        except Exception as exc:
            error_holder[0] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_EXEC_TIMEOUT)

    if thread.is_alive():
        logger.warning("Chart code timed out after %ds", _EXEC_TIMEOUT)
        return None

    if error_holder[0] is not None:
        logger.warning("Chart code failed: %s", error_holder[0])
        return None

    return result_holder[0]
