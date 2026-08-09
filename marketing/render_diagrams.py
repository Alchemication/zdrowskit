"""Pre-render the Mermaid diagrams in `docs/*.md` to static SVG.

Why this is a separate, manually-run tool rather than part of the site build:

- Mermaid's browser bundle is ~3.5 MB. Shipping it to render eight flowcharts
  on one page, or vendoring it into the repo, both cost far more than the
  diagrams are worth.
- Rendering needs a browser. Keeping it out of `marketing/build.py` means CI
  stays Python-only and the published site carries no diagram JavaScript.

So the SVGs are generated here, committed, and merely inlined by the build.
`marketing/build.py` fails loudly if a diagram in the Markdown has no matching
SVG, so a changed diagram cannot silently ship stale.

Run it whenever a ```mermaid block changes:

    uv run python marketing/render_diagrams.py

Requires Google Chrome (or set CHROME_BIN). The Mermaid bundle is downloaded
once into a gitignored cache.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_SRC = REPO_ROOT / "docs"
DIAGRAM_DIR = REPO_ROOT / "marketing" / "site" / "assets" / "diagrams"
CACHE_DIR = REPO_ROOT / "marketing" / ".cache"

# Pinned so a regenerated diagram does not silently change shape with an
# upstream release. Bump deliberately, then re-render and eyeball the diff.
MERMAID_VERSION = "11.12.0"
MERMAID_URL = (
    f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"
)

# Mermaid needs real time to lay out; virtual time lets headless Chrome fast
# forward its timers so the dump still captures finished SVG.
VIRTUAL_TIME_BUDGET_MS = 20000

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]

# Matches the site palette so diagrams read as part of the page rather than as
# embedded screenshots. Stroke weights are handled by the docs stylesheet,
# which can target the inlined SVG directly.
THEME_VARIABLES = {
    "background": "transparent",
    "primaryColor": "#dcd4bd",
    "primaryTextColor": "#15190f",
    "primaryBorderColor": "#15190f",
    "secondaryColor": "#e7e0c9",
    "tertiaryColor": "#eee7d2",
    "lineColor": "#15190f",
    "textColor": "#15190f",
    "fontFamily": "ui-monospace, SFMono-Regular, Menlo, monospace",
    "fontSize": "14px",
}

FENCE_RE = re.compile(
    r"^[ \t]*```mermaid[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE
)


def diagram_id(source: str) -> str:
    """Return the stable identifier for a diagram's source text.

    Content-addressed so that editing a diagram produces a new filename and the
    build notices the old SVG no longer matches anything.

    Args:
        source: The Mermaid source inside the fence.

    Returns:
        A short hex digest.
    """
    return hashlib.sha256(source.strip().encode("utf-8")).hexdigest()[:16]


def collect_diagrams() -> dict[str, str]:
    """Find every Mermaid diagram across the docs.

    Returns:
        A mapping of diagram id to Mermaid source.
    """
    found: dict[str, str] = {}
    for path in sorted(DOCS_SRC.glob("*.md")):
        for match in FENCE_RE.finditer(path.read_text(encoding="utf-8")):
            source = match.group(1).strip()
            found[diagram_id(source)] = source
    return found


def find_chrome() -> str:
    """Locate a Chrome or Chromium binary.

    Returns:
        Path or command name for the browser.

    Raises:
        SystemExit: If no browser can be found.
    """
    override = os.environ.get("CHROME_BIN")
    candidates = [override, *CHROME_CANDIDATES] if override else CHROME_CANDIDATES
    for candidate in candidates:
        if candidate and (Path(candidate).exists() or shutil.which(candidate)):
            return candidate
    print(
        "error: no Chrome or Chromium found.\n"
        "Install Google Chrome, or point CHROME_BIN at a browser binary.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def mermaid_bundle() -> Path:
    """Return the cached Mermaid bundle, downloading it if absent.

    Returns:
        Path to `mermaid.min.js` in the local cache.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bundle = CACHE_DIR / f"mermaid-{MERMAID_VERSION}.min.js"
    if not bundle.exists():
        print(f"Downloading mermaid {MERMAID_VERSION}…")
        with urllib.request.urlopen(MERMAID_URL) as response:  # noqa: S310 - pinned https URL
            bundle.write_bytes(response.read())
    return bundle


def build_harness(diagrams: dict[str, str], bundle: Path) -> str:
    """Build the HTML page that renders every diagram in one browser pass.

    Args:
        diagrams: Mapping of diagram id to Mermaid source.
        bundle: Path to the Mermaid bundle to inline.

    Returns:
        Complete HTML for the harness page.
    """
    import json

    blocks = "\n".join(
        f'<div class="mermaid" id="d-{key}">{source.replace("&", "&amp;").replace("<", "&lt;")}</div>'
        for key, source in diagrams.items()
    )
    config = json.dumps(
        {
            "startOnLoad": True,
            "theme": "base",
            "themeVariables": THEME_VARIABLES,
            "flowchart": {"useMaxWidth": True, "htmlLabels": True},
        }
    )
    return f"""<!doctype html>
<html><body style="margin:0;background:#eee7d2">
{blocks}
<script>{bundle.read_text(encoding="utf-8")}</script>
<script>mermaid.initialize({config});</script>
</body></html>
"""


def extract_svgs(dom: str, keys: list[str]) -> dict[str, str]:
    """Pull each rendered SVG out of the dumped DOM.

    Args:
        dom: Serialized DOM from headless Chrome.
        keys: Diagram ids to look for.

    Returns:
        Mapping of diagram id to SVG markup, omitting any that did not render.
    """
    svgs: dict[str, str] = {}
    for key in keys:
        anchor = dom.find(f'id="d-{key}"')
        if anchor == -1:
            continue
        start = dom.find("<svg", anchor)
        end = dom.find("</svg>", start)
        if start == -1 or end == -1:
            continue
        svgs[key] = dom[start : end + len("</svg>")]
    return svgs


def clean_svg(svg: str, key: str) -> str:
    """Make a rendered SVG safe to inline into a page.

    Mermaid emits a per-run random id and an inline `max-width` sized to the
    browser viewport. Both have to go: the id would change on every re-render
    and churn the committed file, and the width would freeze the diagram at
    whatever the harness happened to be.

    Args:
        svg: Raw SVG markup from the browser.
        key: Diagram id, used for a stable element id.

    Returns:
        Cleaned SVG markup.
    """
    svg = re.sub(r'\sid="mermaid-[^"]*"', f' id="diagram-{key}"', svg, count=1)
    svg = re.sub(r'\sstyle="max-width:[^"]*"', "", svg, count=1)
    svg = re.sub(r'\swidth="100%"', "", svg, count=1)

    # Pin the intrinsic size from the viewBox. Without it the SVG scales to its
    # container, and the denser flowcharts shrink until the labels are
    # unreadable; at natural size they scroll inside the figure instead.
    viewbox = re.search(r'viewBox="([\d.\-\s]+)"', svg)
    if viewbox:
        parts = viewbox.group(1).split()
        if len(parts) == 4:
            width, height = float(parts[2]), float(parts[3])
            svg = svg.replace(
                "<svg ", f'<svg width="{width:.0f}" height="{height:.0f}" ', 1
            )

    svg = svg.replace("<svg ", '<svg class="mermaid-svg" ', 1)
    # Mermaid scopes its CSS with the random id; rewrite those selectors too.
    svg = re.sub(r"#mermaid-\d+", f"#diagram-{key}", svg)

    # Layout metadata the browser never reads, but which is a large share of
    # the file and churns on every re-render.
    svg = re.sub(r'\sdata-points="[^"]*"', "", svg)

    # Mermaid emits full float precision in path data, which is roughly two
    # thirds of an unoptimised diagram. Two decimals is far below one screen
    # pixel and keeps re-render diffs readable.
    def round_path(match: re.Match[str]) -> str:
        rounded = re.sub(
            r"-?\d+\.\d+",
            lambda number: f"{float(number.group()):.2f}".rstrip("0").rstrip("."),
            match.group(1),
        )
        return f'd="{rounded}"'

    return re.sub(r'd="([^"]*)"', round_path, svg)


def render(diagrams: dict[str, str]) -> dict[str, str]:
    """Render all diagrams to SVG with headless Chrome.

    Args:
        diagrams: Mapping of diagram id to Mermaid source.

    Returns:
        Mapping of diagram id to cleaned SVG markup.

    Raises:
        SystemExit: If the browser renders nothing.
    """
    chrome = find_chrome()
    harness_html = build_harness(diagrams, mermaid_bundle())

    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.html"
        harness.write_text(harness_html, encoding="utf-8")
        result = subprocess.run(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
                "--dump-dom",
                harness.as_uri(),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    svgs = extract_svgs(result.stdout, list(diagrams))
    if not svgs:
        print(
            "error: the browser produced no SVG. Run without --dump-dom to see "
            "the page, or check that the Mermaid sources parse.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return {key: clean_svg(svg, key) for key, svg in svgs.items()}


def main() -> None:
    """CLI entry point for diagram rendering."""
    parser = argparse.ArgumentParser(
        description="Pre-render docs Mermaid diagrams to SVG."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any diagram is missing an SVG, rendering nothing.",
    )
    args = parser.parse_args()

    diagrams = collect_diagrams()
    if not diagrams:
        print("No mermaid blocks found in docs/.")
        return

    DIAGRAM_DIR.mkdir(parents=True, exist_ok=True)
    if args.check:
        missing = [key for key in diagrams if not (DIAGRAM_DIR / f"{key}.svg").exists()]
        if missing:
            print(
                f"error: {len(missing)} diagram(s) have no rendered SVG.\n"
                "Run: uv run python marketing/render_diagrams.py",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(f"All {len(diagrams)} diagram(s) have an SVG.")
        return

    svgs = render(diagrams)
    for key, svg in svgs.items():
        (DIAGRAM_DIR / f"{key}.svg").write_text(svg, encoding="utf-8")

    # Drop SVGs whose source no longer exists, so edits do not leave orphans.
    removed = 0
    for existing in DIAGRAM_DIR.glob("*.svg"):
        if existing.stem not in diagrams:
            existing.unlink()
            removed += 1

    failed = sorted(set(diagrams) - set(svgs))
    for key in failed:
        print(f"warning: {key} did not render", file=sys.stderr)
    print(
        f"Rendered {len(svgs)}/{len(diagrams)} diagram(s) to "
        f"{DIAGRAM_DIR.relative_to(REPO_ROOT)}"
        + (f", removed {removed} orphan(s)" if removed else "")
    )


if __name__ == "__main__":
    main()
