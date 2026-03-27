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
            result_holder[0] = fig.to_image(
                format="png", width=900, height=450, scale=2
            )
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
