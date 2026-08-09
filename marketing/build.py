"""Build the zdrowskit public site into `_site/`.

Three sources, one output tree:

- `marketing/site/` — the hand-written landing page and its assets.
- `docs/*.md` — the repo's own docs, rendered to HTML so the published site and
  the repo can never drift apart. There is no second copy to maintain.
- `evals/leaderboard/runs.jsonl` — rendered by the existing leaderboard module.

Run it with the `site` dependency group:

    uv run --group site python marketing/build.py
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

# Sibling module: both live in marketing/, which is sys.path[0] when this file
# is run as a script. Importing the id function rather than reimplementing it
# keeps the build and the renderer agreeing on which SVG belongs to which fence.
from render_diagrams import DIAGRAM_DIR, diagram_id

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = REPO_ROOT / "marketing" / "site"
DOCS_SRC = REPO_ROOT / "docs"
DEFAULT_OUT = REPO_ROOT / "_site"

GITHUB_REPO = "https://github.com/Alchemication/zdrowskit"
GITHUB_BLOB = f"{GITHUB_REPO}/blob/main"

# Docs are listed in this order when one is present; anything not named here is
# appended alphabetically. Ordering is editorial — setup before reference —
# rather than derived from the filesystem, which would surface `apple-health`
# first purely by alphabet.
DOCS_ORDER = [
    "setup",
    "apple-health",
    "http-ingest",
    "google-drive",
    "telegram",
    "llm",
    "daemon",
    "commands",
    "context-files",
    "multi-profile",
    "family-hosting",
    "notifications",
    "user-flow",
    "evals",
    "testing",
    "limitations",
]

# One-line blurbs for the docs index. A doc with no entry falls back to its
# first paragraph, which is usually serviceable but rarely as tight.
DOCS_BLURBS = {
    "setup": "Install, configure, and get the first report out.",
    "apple-health": "Export shapes, transports, and which one to pick.",
    "http-ingest": "The default transport: HTTPS ingest over Tailscale Funnel.",
    "google-drive": "The alternative transport, and when it is the right call.",
    "telegram": "Bot setup, chat routing, and the approve/reject buttons.",
    "llm": "Providers, model choice, reasoning effort, and what each call costs.",
    "daemon": "The always-on process: schedules, watchers, and service install.",
    "commands": "Every CLI subcommand, with flags.",
    "context-files": "Goals, plan, injuries, journal — the files the coach reads.",
    "multi-profile": "Profile scoping, isolated databases, and the TOML roster.",
    "family-hosting": "Hosting people you know: trust model and operator duties.",
    "notifications": "Delivery rules, quiet hours, and nudge budgets.",
    "user-flow": "What a week actually looks like from the user's side.",
    "evals": "The eval harness, case kinds, and how the leaderboard is recorded.",
    "testing": "Running the suite and what must have coverage.",
    "limitations": "What this does not do, and who should not use it.",
}


# The landing page shows the user-facing surfaces only, in narrative order:
# the report, the checker that reads it, the two things that reach you, and the
# one route that is not backed by measurement. The other four features in
# `model_prefs` are utility calls nobody asks about.
ROUTING_FEATURES: tuple[str, ...] = (
    "insights",
    "verification",
    "chat",
    "nudge",
    "coach",
)

# Why each route is what it is, in the reader's terms. Model names and
# fallbacks are read from the code so they cannot go stale; this cannot be, so
# the build fails rather than shipping a surface with no explanation.
ROUTING_WHY: dict[str, str] = {
    "insights": "Judgement work, but a frontier model measured no better here — and cost 60x more.",
    "verification": "Measured best at catching bad claims: 85.7% against 57.1% for both premium options. Being cheap is why it can check every report rather than a sample.",
    "chat": "You are waiting on this one. Seven seconds beats thirty.",
    "nudge": "Scored 80% against 40% for three pricier models, and answers in under five seconds.",
    "coach": "No eval coverage — this route was chosen on price, not evidence.",
}

# Routes with no measurement behind them. Called out in the table rather than
# blended in: a scorecard that hides its own gaps is not worth publishing.
ROUTING_GAPS: frozenset[str] = frozenset({"coach"})


@dataclass(frozen=True)
class Doc:
    """A rendered documentation page.

    Attributes:
        slug: Output filename stem, matching the source Markdown stem.
        title: Page title taken from the first H1, or a prettified slug.
        body: Rendered HTML body.
        blurb: One-line summary for the docs index.
    """

    slug: str
    title: str
    body: str
    blurb: str


def read_base_css() -> str:
    """Read the shared palette and site chrome.

    Every page inlines this rather than linking it, so each output file stays
    self-contained while the palette has exactly one definition. The leaderboard
    reads the same file directly — see `_load_base_css` in
    `evals/leaderboard/html.py`.

    Returns:
        The stylesheet text.

    Raises:
        SystemExit: If the stylesheet is missing.
    """
    path = SITE_SRC / "assets" / "base.css"
    if not path.exists():
        print(
            f"error: {path.relative_to(REPO_ROOT)} is missing.\n"
            "Every page of the site inlines it for the palette and chrome. "
            "Restore it, or update read_base_css() if it moved.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return path.read_text(encoding="utf-8")


def rewrite_links(rendered: str, *, in_docs: bool) -> str:
    """Point Markdown-relative links at their built equivalents.

    Args:
        rendered: Rendered HTML fragment.
        in_docs: True when the fragment will live under `/docs/`, which makes
            sibling `foo.md` links resolve to `foo.html` in the same directory.

    Returns:
        The fragment with `.md` hrefs rewritten and repo-relative source paths
        pointed at GitHub.
    """

    def replace(match: re.Match[str]) -> str:
        href = match.group(1)
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)

        target, _, anchor = href.partition("#")
        suffix = f"#{anchor}" if anchor else ""

        if target.endswith(".md"):
            stem = Path(target).stem
            if stem == "README":
                # The landing page is the README's public face. Section anchors
                # from the README have no counterpart there, so they are dropped
                # rather than left pointing at an id that does not exist.
                return f'href="{"../" if in_docs else "./"}"'
            relative = (
                f"{stem}.html{suffix}" if in_docs else f"docs/{stem}.html{suffix}"
            )
            return f'href="{relative}"'

        # Repo-relative source paths (src/llm.py, evals/cases/...) only exist on
        # GitHub, never in the built tree.
        if target and not target.startswith("/"):
            return f'href="{GITHUB_BLOB}/{target}{suffix}"'
        return match.group(0)

    return re.sub(r'href="([^"]+)"', replace, rendered)


def render_markdown(md: MarkdownIt, source: str, *, in_docs: bool) -> tuple[str, str]:
    """Render Markdown to an HTML body and extract its title.

    Args:
        md: Configured markdown-it renderer.
        source: Markdown text.
        in_docs: Passed through to link rewriting.

    Returns:
        A `(title, body_html)` pair. Title is the first H1 if present, else "".
    """
    body = rewrite_links(md.render(source), in_docs=in_docs)
    heading = re.search(r"^#\s+(.+)$", source, re.MULTILINE)
    title = heading.group(1).strip() if heading else ""
    # The H1 is reprinted by the page shell, so drop the duplicate from the body.
    body = re.sub(r"<h1>.*?</h1>\s*", "", body, count=1, flags=re.DOTALL)
    return title, body


def first_paragraph(source: str) -> str:
    """Return the first non-heading, non-quote paragraph as plain-ish text.

    Args:
        source: Markdown text.

    Returns:
        A trimmed one-line summary, or "" when nothing suitable is found.
    """
    for block in source.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(("#", ">", "-", "*", "|", "```")):
            continue
        flat = " ".join(block.split())
        flat = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", flat)
        flat = flat.replace("`", "")
        return flat if len(flat) <= 160 else flat[:157].rsplit(" ", 1)[0] + "…"
    return ""


def page_shell(
    *, title: str, description: str, base_css: str, content: str, depth: int
) -> str:
    """Wrap rendered content in the shared docs chrome.

    Args:
        title: Page title, used for `<title>` and the visible H1.
        description: Meta description text.
        base_css: The shared stylesheet, inlined into the page.
        content: HTML body content.
        depth: Directory depth below the site root, for relative asset paths.

    Returns:
        A complete HTML document.
    """
    up = "../" * depth
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — zdrowskit</title>
<meta name="description" content="{html.escape(description)}">
<link rel="icon" href="{up}assets/favicon.svg" type="image/svg+xml">
<style>
{base_css}
/* Docs-specific: prose column and long-form typography. */
body {{ line-height: 1.7; }}
a {{ color: var(--green-dark); }}
.shell {{ width: min(860px, calc(100% - 32px)); }}
main {{ padding: 48px 0 80px; }}
h1 {{
  margin: 0 0 8px; font-family: Georgia, "Times New Roman", serif;
  font-size: clamp(34px, 5vw, 52px); font-weight: 500; line-height: 1.02; letter-spacing: -.03em;
}}
h2 {{
  margin: 44px 0 14px; padding-top: 18px; border-top: 2px solid var(--ink);
  font-family: Georgia, serif; font-size: 26px; font-weight: 500; letter-spacing: -.02em;
}}
h3 {{ margin: 30px 0 10px; font-size: 13px; font-weight: 900; letter-spacing: .08em; text-transform: uppercase; }}
p, li {{ color: #3c3f33; }}
pre {{
  overflow-x: auto; padding: 16px 18px; border: 2px solid var(--ink);
  background: var(--ink); color: var(--paper); box-shadow: 4px 4px 0 var(--ink); line-height: 1.55;
}}
pre code {{ padding: 0; border: 0; background: none; color: inherit; }}
blockquote {{
  margin: 20px 0; padding: 4px 0 4px 18px;
  border-left: 5px solid var(--orange); color: var(--muted);
}}
table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }}
th, td {{ padding: 9px 11px; border: 1px solid rgba(21,25,15,.3); text-align: left; vertical-align: top; }}
th {{ background: rgba(21,25,15,.06); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; }}
.table-scroll {{ overflow-x: auto; }}
/* Pre-rendered Mermaid, inlined as SVG. Flowcharts are much wider than a
   comfortable prose measure, so the figure breaks out of the text column to
   the full site width — the difference between a legible diagram and a
   thumbnail. Anything still too wide scrolls inside the figure. */
.diagram {{
  position: relative;
  left: 50%;
  transform: translateX(-50%);
  width: min(1180px, calc(100vw - 32px));
  overflow-x: auto;
  margin: 30px 0;
  padding: 20px 18px;
  border: 2px solid var(--ink);
  background: rgba(255,255,255,.3);
  box-shadow: var(--shadow);
}}
/* The SVG carries its intrinsic size, so small diagrams sit at natural scale
   and only oversized ones are scaled down to the frame. */
.diagram svg {{ display: block; max-width: 100%; height: auto; margin: 0 auto; }}
.diagram .node rect, .diagram .node polygon,
.diagram .node circle, .diagram .node path {{ stroke-width: 2px; }}
.diagram .edgePath .path, .diagram .flowchart-link {{ stroke-width: 1.8px; }}
.lede {{ margin: 0 0 34px; color: var(--muted); }}
.doc-list {{ list-style: none; margin: 0; padding: 0; }}
.doc-list li {{ padding: 15px 0; border-bottom: 1px solid rgba(21,25,15,.22); }}
.doc-list a {{ font-weight: 800; text-decoration: none; }}
.doc-list a:hover {{ color: var(--orange); }}
.doc-list span {{ display: block; color: var(--muted); font-size: 12px; }}
</style>
</head>
<body>
<header class="shell topbar">
  <a class="wordmark" href="{up}">ZDROW<span>//</span>SKIT</a>
  <nav>
    <a href="{up}docs/">Docs</a>
    <a href="{up}evals/">Evals</a>
    <a href="{GITHUB_REPO}">GitHub</a>
  </nav>
</header>
<main class="shell">
<h1>{html.escape(title)}</h1>
{content}
</main>
<footer class="shell">
  <span>ZDROWSKIT</span>
  <span><a href="{GITHUB_BLOB}/docs">Edit these docs on GitHub</a></span>
</footer>
</body>
</html>
"""


def wrap_tables(body: str) -> str:
    """Wrap tables so wide ones scroll instead of widening the page.

    Args:
        body: Rendered HTML.

    Returns:
        HTML with each `<table>` inside a horizontally scrollable container.
    """
    return body.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>"
    )


def install_mermaid_renderer(md: MarkdownIt) -> None:
    """Make ```mermaid fences render as their pre-built SVG.

    The diagrams are rendered ahead of time by `marketing/render_diagrams.py`
    so the published page carries no Mermaid runtime. The Markdown keeps the
    Mermaid source, which is what GitHub renders natively.

    Args:
        md: The renderer to patch.
    """
    default_fence = md.renderer.rules.get("fence")

    def fence(tokens: list, idx: int, options: dict, env: dict) -> str:
        token = tokens[idx]
        if token.info.strip() != "mermaid":
            return default_fence(tokens, idx, options, env)

        key = diagram_id(token.content)
        svg_path = DIAGRAM_DIR / f"{key}.svg"
        if not svg_path.exists():
            print(
                f"error: a mermaid diagram has no pre-rendered SVG ({key}.svg).\n"
                "The diagram source changed since it was last rendered. Run:\n"
                "  uv run python marketing/render_diagrams.py",
                file=sys.stderr,
            )
            raise SystemExit(1)
        svg = svg_path.read_text(encoding="utf-8")
        return f'<figure class="diagram">{svg}</figure>\n'

    md.renderer.rules["fence"] = fence


def build_docs(out: Path, base_css: str) -> list[Doc]:
    """Render every `docs/*.md` into `_site/docs/`.

    Args:
        out: Site output root.
        base_css: Shared stylesheet, inlined into each page.

    Returns:
        The rendered docs, in display order.
    """
    md = MarkdownIt("commonmark", {"html": False, "linkify": True})
    md.enable(["table", "strikethrough"])
    install_mermaid_renderer(md)

    sources = sorted(DOCS_SRC.glob("*.md"))
    order = {slug: i for i, slug in enumerate(DOCS_ORDER)}
    sources.sort(key=lambda p: (order.get(p.stem, len(order)), p.stem))

    docs_out = out / "docs"
    docs_out.mkdir(parents=True, exist_ok=True)

    docs: list[Doc] = []
    for path in sources:
        source = path.read_text(encoding="utf-8")
        title, body = render_markdown(md, source, in_docs=True)
        title = title or path.stem.replace("-", " ").title()
        blurb = DOCS_BLURBS.get(path.stem) or first_paragraph(source)
        doc = Doc(slug=path.stem, title=title, body=wrap_tables(body), blurb=blurb)
        docs.append(doc)
        (docs_out / f"{doc.slug}.html").write_text(
            page_shell(
                title=doc.title,
                description=blurb or f"zdrowskit documentation: {doc.title}",
                base_css=base_css,
                content=doc.body,
                depth=1,
            ),
            encoding="utf-8",
        )

    items = "\n".join(
        f'  <li><a href="{d.slug}.html">{html.escape(d.title)}</a>'
        f"<span>{html.escape(d.blurb)}</span></li>"
        for d in docs
    )
    index = (
        '<p class="lede">Rendered from the repository\'s own <code>docs/</code> '
        "directory on every push, so these pages and the code never drift apart.</p>\n"
        f'<ul class="doc-list">\n{items}\n</ul>'
    )
    (docs_out / "index.html").write_text(
        page_shell(
            title="Documentation",
            description="Setup, transports, LLM configuration, and operations for zdrowskit.",
            base_css=base_css,
            content=index,
            depth=1,
        ),
        encoding="utf-8",
    )
    return docs


def landing_placeholders() -> dict[str, str]:
    """Resolve `{{...}}` tokens in the landing page from recorded eval history.

    The landing page cites how many eval cases exist and when they were last
    scored. Hard-coding either guarantees the page eventually lies, so both are
    read from the same `runs.jsonl` the leaderboard renders.

    Returns:
        A mapping of placeholder name to replacement text.
    """
    runs = REPO_ROOT / "evals" / "leaderboard" / "runs.jsonl"
    if not runs.exists():
        return {"EVAL_CASE_COUNT": "30", "EVAL_UPDATED": "recently"}

    records = [
        json.loads(line)
        for line in runs.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        return {"EVAL_CASE_COUNT": "30", "EVAL_UPDATED": "recently"}

    latest = max(records, key=lambda r: str(r.get("created_at", "")))
    case_ids: set[str] = set()
    for record in records:
        if str(record.get("created_at", "")) == str(latest.get("created_at", "")):
            case_ids.update(record.get("case_ids", []))
    count = len(case_ids) or int(latest.get("case_count", 0))
    created = str(latest.get("created_at", ""))[:10] or "recently"
    return {"EVAL_CASE_COUNT": str(count), "EVAL_UPDATED": created}


def routing_table() -> str:
    """Render the per-feature model routing as an HTML table.

    Model names, fallbacks and reasoning levels come from
    `model_prefs.default_model_prefs()` so the published table always matches
    the shipped defaults — the routes changed four times in three days once,
    and a hand-written copy would have been wrong within a week. It reads
    defaults rather than any profile, so it never shows a local override.

    Returns:
        An HTML `<table>`.

    Raises:
        SystemExit: If a listed feature has no entry in `ROUTING_WHY`.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    import model_prefs

    defaults = model_prefs.default_model_prefs()["features"]

    missing = [f for f in ROUTING_FEATURES if f not in ROUTING_WHY]
    if missing:
        print(
            f"error: no ROUTING_WHY entry for: {', '.join(missing)}.\n"
            "Every routed surface on the landing page needs a plain-language "
            "reason. Add one in marketing/build.py.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    rows = []
    for feature in ROUTING_FEATURES:
        entry = defaults.get(feature, {})
        label = model_prefs.FEATURE_LABELS.get(feature, feature)
        model = model_prefs.model_label(str(entry.get("primary", "—")))
        gap = feature in ROUTING_GAPS
        rows.append(
            f"    <tr{' class="gap"' if gap else ''}>"
            f"<td>{html.escape(label)}</td>"
            f"<td><code>{html.escape(model)}</code></td>"
            f"<td>{html.escape(ROUTING_WHY[feature])}</td></tr>"
        )
    body = "\n".join(rows)
    return (
        '<div class="routing-scroll">\n  <table class="routing">\n'
        "    <thead><tr><th>Job</th><th>Model</th><th>Why this one</th></tr></thead>\n"
        f"    <tbody>\n{body}\n    </tbody>\n"
        "  </table>\n</div>"
    )


def render_landing(out: Path, base_css: str) -> None:
    """Copy the landing page, substituting its build-time placeholders.

    Args:
        out: Site output root.
        base_css: Shared stylesheet, inlined in place of `{{BASE_CSS}}`.

    Raises:
        SystemExit: If any `{{PLACEHOLDER}}` survives substitution, which would
            otherwise publish literal braces to visitors.
    """
    page = (SITE_SRC / "index.html").read_text(encoding="utf-8")
    substitutions = {
        "BASE_CSS": base_css,
        "ROUTING_TABLE": routing_table(),
        **landing_placeholders(),
    }
    for name, value in substitutions.items():
        page = page.replace(f"{{{{{name}}}}}", value)

    leftover = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", page)))
    if leftover:
        print(
            f"error: unresolved placeholder(s) in the landing page: {', '.join(leftover)}.\n"
            "Add them to landing_placeholders() or remove them from "
            "marketing/site/index.html.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    (out / "index.html").write_text(page, encoding="utf-8")


def build_leaderboard(out: Path) -> bool:
    """Render the eval leaderboard into `_site/evals/`.

    Args:
        out: Site output root.

    Returns:
        True if the leaderboard rendered, False if its run history is missing.
    """
    runs = REPO_ROOT / "evals" / "leaderboard" / "runs.jsonl"
    evals_out = out / "evals"
    evals_out.mkdir(parents=True, exist_ok=True)

    if not runs.exists():
        print(f"warning: {runs} not found; skipping the leaderboard.", file=sys.stderr)
        return False

    for args in (
        # nav-base gives the leaderboard the shared header and footer, pointing
        # one level up at the site root. The local artifact is rendered without
        # it, since its sibling docs/ holds Markdown rather than built pages.
        [
            "render-html",
            "--html-path",
            str(evals_out / "index.html"),
            "--nav-base",
            "../",
        ],
        ["render", "--markdown-path", str(evals_out / "leaderboard.md")],
    ):
        subprocess.run(
            [sys.executable, "-m", "evals.leaderboard", *args],
            cwd=REPO_ROOT,
            check=True,
        )
    shutil.copy2(runs, evals_out / "runs.jsonl")
    return True


def build(out: Path) -> None:
    """Build the whole site.

    Args:
        out: Site output root; removed and recreated.
    """
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    base_css = read_base_css()

    render_landing(out, base_css)
    shutil.copytree(SITE_SRC / "assets", out / "assets")

    docs = build_docs(out, base_css)
    has_leaderboard = build_leaderboard(out)

    # Pages would otherwise run Jekyll, which skips files beginning with "_".
    (out / ".nojekyll").write_text("", encoding="utf-8")

    print(
        f"Built {out.relative_to(REPO_ROOT)}: landing + {len(docs)} doc page(s)"
        f"{' + leaderboard' if has_leaderboard else ''}"
    )


def main() -> None:
    """CLI entry point for the site build."""
    parser = argparse.ArgumentParser(description="Build the zdrowskit public site.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    build(args.out.resolve())


if __name__ == "__main__":
    main()
