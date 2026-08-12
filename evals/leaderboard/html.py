"""Interactive HTML rendering for the eval leaderboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.framework import EvalCase
from evals.leaderboard.record import HTML_PATH
from evals.leaderboard.scorecard import build_scorecard

_TITLE = "Eval Leaderboard"
_NOTE = (
    "Every part of zdrowskit that talks to a language model is scored against "
    "a fixed set of test cases: a frozen input — a date, the health data, the "
    "conversation so far — plus checks on what the answer must and must not "
    "say. Most cases are recorded from real failures. Every score is published "
    "here, including the bad ones."
)
_GITHUB_REPO = "https://github.com/Alchemication/zdrowskit"

# The published page has to stand on its own: a reader who has never seen the
# codebase still meets "strict", "flaky" and "repeat" in the first table. Each
# entry defines the term in the terms of the thing being measured, not in the
# vocabulary of the harness.
_GLOSSARY = (
    (
        "Case",
        "One frozen input plus the checks on its answer — for example: given "
        "this week's data, the weekly report must not claim HRV rose when it "
        "fell.",
    ),
    (
        "Feature",
        "The part of the product under test. Each one is scored separately, on "
        "its own cases and its own model.",
    ),
    (
        "Repeat",
        "How many times each case was run. The same input does not produce the "
        "same answer twice, so one run is a sample, not a verdict.",
    ),
    (
        "Strict",
        "The share of cases that passed <em>every</em> attempt. This is the "
        "headline score, and the harsh one.",
    ),
    (
        "Attempt",
        "The share of individual attempts that passed — the score a single run "
        "would be expected to report. It sits above strict whenever some cases "
        "only pass sometimes.",
    ),
    (
        "Flaky",
        "Cases that passed on some attempts and failed on others. The most "
        "dangerous result there is: one run reports it as a clean pass or a "
        "clean failure with equal confidence.",
    ),
    (
        "Route",
        "The model that answers, its reasoning level, and the fallback used if "
        "the provider fails. Written <code>model (reasoning) -&gt; fallback</code>.",
    ),
    (
        "Tool calls",
        "How many times the model went and looked something up before "
        "answering — averaged per attempt. <em>Varied</em> counts cases that "
        "took a different path on a rerun: same question, same verdict, "
        "different route to it.",
    ),
    (
        "Cost / run",
        "Average spend to run that feature's whole case set once, in USD. "
        "Normalised by repeat, so a 5-sample row is not five times the price of "
        "a 1-sample one.",
    ),
    (
        "Recorded",
        "The date and commit the run was measured at. Scores move when prompts "
        "and models move, so an old row describes old code.",
    ),
)

# The shared palette and site chrome. Inlined rather than linked so the rendered
# page stays a single self-contained file that works from disk as well as from
# the published site.
_BASE_CSS_PATH = (
    Path(__file__).resolve().parents[2] / "marketing" / "site" / "assets" / "base.css"
)


def _load_base_css() -> str:
    """Read the shared site stylesheet.

    Returns:
        The stylesheet text, or "" if it is missing, in which case the page
        still renders with its own rules and browser defaults.
    """
    try:
        return _BASE_CSS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _site_chrome(nav_base: str) -> tuple[str, str]:
    """Build the shared site header and footer for the published leaderboard.

    Args:
        nav_base: Relative path from this page back to the site root, e.g. `../`.

    Returns:
        A `(header, footer)` pair of HTML strings.
    """
    header = f"""  <header class="shell topbar">
    <a class="wordmark" href="{nav_base}">ZDROW<span>//</span>SKIT</a>
    <nav>
      <a href="{nav_base}docs/">Docs</a>
      <a href="{nav_base}evals/">Evals</a>
      <a href="{_GITHUB_REPO}">GitHub</a>
    </nav>
  </header>
"""
    footer = f"""  <footer class="shell">
    <span>ZDROWSKIT</span>
    <span><a href="{_GITHUB_REPO}/blob/main/docs/evals.md">How these are measured</a></span>
  </footer>
"""
    return header, footer


def _glossary_html() -> str:
    """Render the always-visible legend for the page's vocabulary."""
    items = "\n".join(
        f"      <div><dt>{term}</dt><dd>{definition}</dd></div>"
        for term, definition in _GLOSSARY
    )
    return (
        '  <section class="legend">\n'
        "    <h2>How to read this page</h2>\n"
        f'    <dl class="legend-grid">\n{items}\n    </dl>\n'
        "  </section>\n"
    )


def render_leaderboard_html(
    runs: list[dict[str, Any]],
    *,
    inventory: list[EvalCase] | None = None,
    head_sha: str | None = None,
    nav_base: str | None = None,
) -> str:
    """Render an interactive HTML leaderboard from persisted run records.

    Args:
        runs: Persisted run records.
        inventory: Current eval cases, used to flag runs recorded before a case
            existed.
        head_sha: Current commit, recorded in the payload.
        nav_base: Relative path to the site root, which adds the shared site
            header and footer. Omit for the standalone local artifact, whose
            sibling `docs/` holds Markdown rather than the built pages.

    Returns:
        A complete, self-contained HTML document.
    """
    payload = build_scorecard(runs, inventory=inventory, head_sha=head_sha)
    empty = (
        '<div class="empty-state">'
        "<h2>No recorded eval runs yet</h2>"
        "<p>Use <code>uv run python -m evals.run --repeat 3 --record</code> "
        "to add the first run.</p>"
        "</div>"
    )
    app = '<div id="app"></div>' if runs else empty
    data_json = json.dumps(payload, sort_keys=True).replace("</", "<\\/")
    header, footer = _site_chrome(nav_base) if nav_base is not None else ("", "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_TITLE} — zdrowskit</title>
  <meta name="description" content="Regression scores for every zdrowskit LLM feature, published in full — what was tested, what passed, and what is still flaky.">
  <style>{_load_base_css()}{_STYLE}</style>
</head>
<body>
{header}  <main class="page shell">
    <section class="hero">
      <div class="eyebrow">Eval Leaderboard</div>
      <h1>{_TITLE}</h1>
      <div class="lede">{_NOTE}</div>
    </section>
{_glossary_html()}    {app}
  </main>
{footer}  <script id="leaderboard-data" type="application/json">{data_json}</script>
  <script>{_SCRIPT}</script>
</body>
</html>"""


def write_leaderboard_html(
    runs: list[dict[str, Any]],
    html_path: Path | None = None,
    *,
    inventory: list[EvalCase] | None = None,
    head_sha: str | None = None,
    nav_base: str | None = None,
) -> str:
    """Write the rendered leaderboard HTML to disk.

    Args:
        runs: Persisted run records.
        html_path: Destination path; defaults to the repo's `HTML_PATH`.
        inventory: Current eval cases.
        head_sha: Current commit, recorded in the payload.
        nav_base: Relative path to the site root; see `render_leaderboard_html`.

    Returns:
        The rendered HTML that was written.
    """
    path = html_path or HTML_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_leaderboard_html(
        runs,
        inventory=inventory,
        head_sha=head_sha,
        nav_base=nav_base,
    )
    path.write_text(content, encoding="utf-8")
    return content


# Leaderboard-specific rules only. The palette and the site chrome (body,
# .shell, .topbar, footer) come from `marketing/site/assets/base.css`, which is
# inlined ahead of this block — see `_load_base_css`. Do not redeclare tokens
# here; that is how the page drifts away from the rest of the site.
_STYLE = """
    /* Score semantics. Only this page grades a number, so these live here
       rather than in the shared base. */
    :root { --good: #2e4a25; --warn: #8f3d10; --bad: #a8280f; }
    html, body { width: 100%; overflow-x: clip; }
    .shell { width: min(1480px, calc(100% - 32px)); }
    a { color: var(--green-dark); }
    .page { padding: clamp(28px, 3vw, 44px) 0 56px; }
    .hero { display: grid; gap: 12px; margin-bottom: 30px; }
    .hero .eyebrow { justify-self: start; }
    h1 {
      margin: 0;
      font-size: clamp(34px, 5vw, 62px);
      line-height: .95;
      letter-spacing: -.04em;
      font-family: Georgia, "Times New Roman", serif;
      font-weight: 500;
    }
    .lede { max-width: 900px; color: var(--muted); line-height: 1.65; }
    .legend {
      padding: 20px 22px 6px;
      border: 2px solid var(--ink);
      background: rgba(255,255,255,.22);
      box-shadow: var(--shadow);
    }
    .legend h2 {
      margin: 0 0 14px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: var(--green-dark);
      font-weight: 900;
    }
    .legend-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(255px, 1fr));
      gap: 4px 26px;
      margin: 0;
    }
    .legend-grid > div { padding: 9px 0; border-top: 1px solid var(--line); }
    .legend-grid dt {
      font-size: 12px;
      font-weight: 900;
      letter-spacing: .05em;
      text-transform: uppercase;
    }
    .legend-grid dd {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.55;
    }
    .section-title {
      margin: 40px 0 14px;
      padding-top: 18px;
      border-top: 2px solid var(--ink);
      font-family: Georgia, serif;
      font-size: 30px;
      font-weight: 500;
      letter-spacing: -.02em;
    }
    .section-sub { margin: -4px 0 18px; color: var(--muted); font-size: 13px; line-height: 1.6; }
    .prod-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }
    .prod-card {
      background: rgba(255,255,255,.22);
      border: 2px solid var(--ink);
      box-shadow: var(--shadow);
      padding: 18px;
      display: grid;
      gap: 8px;
    }
    .prod-card.missing { border-style: dashed; box-shadow: none; opacity: .8; }
    .prod-card h3 {
      margin: 0;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: var(--green-dark);
    }
    .prod-blurb { margin: -2px 0 4px; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .prod-score { margin: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .prod-headline {
      font-family: Georgia, serif;
      font-size: 34px;
      line-height: 1;
      letter-spacing: -.03em;
    }
    .prod-headline.good { color: var(--good); }
    .prod-headline.warn { color: var(--warn); }
    .prod-headline.bad { color: var(--bad); }
    /* Every number on the card is followed by the count it came from: a bare
       percentage is the thing readers reported not being able to interpret. */
    .prod-facts { margin: 4px 0 0; display: grid; gap: 7px; font-size: 12px; }
    .prod-facts > div { display: grid; grid-template-columns: 74px minmax(0, 1fr); gap: 10px; }
    .prod-facts dt {
      color: var(--green-dark);
      font-size: 10.5px;
      font-weight: 900;
      letter-spacing: .08em;
      text-transform: uppercase;
      padding-top: 2px;
    }
    .prod-facts dd { margin: 0; color: var(--muted); line-height: 1.5; }
    .prod-meta { color: var(--muted); font-size: 12px; line-height: 1.6; }
    .warnings { display: grid; gap: 10px; margin-top: 16px; }
    .warning {
      padding: 14px 16px;
      border: 2px solid var(--orange);
      background: rgba(229,110,54,.12);
      box-shadow: 4px 4px 0 var(--orange);
      color: #7c3208;
      font-size: 12.5px;
      line-height: 1.6;
    }
    .warning.bad {
      border-color: var(--bad);
      background: rgba(168,40,15,.10);
      box-shadow: 4px 4px 0 var(--bad);
      color: #8d2010;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(260px, 300px) minmax(0, 1fr);
      gap: 24px;
      align-items: start;
      min-width: 0;
    }
    .sidebar, .content-card {
      background: rgba(255,255,255,.22);
      border: 2px solid var(--ink);
      box-shadow: var(--shadow);
    }
    .sidebar, .content, .content-card, .table-wrap { min-width: 0; }
    .sidebar { position: sticky; top: 18px; padding: 20px; display: grid; gap: 18px; }
    .sidebar h2, .content-card h2 {
      margin: 0;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: var(--green-dark);
      font-weight: 900;
    }
    .filter-grid { display: grid; gap: 14px; }
    .field { display: grid; gap: 6px; }
    .field label, .toggle { font-size: 12px; color: var(--muted); font-weight: 700; }
    select, input[type="search"] {
      width: 100%;
      border: 2px solid var(--ink);
      background: var(--paper);
      color: var(--ink);
      padding: 10px 12px;
      font: inherit;
      font-size: 12.5px;
    }
    select:focus-visible, input[type="search"]:focus-visible {
      outline: 2px solid var(--orange);
      outline-offset: 1px;
    }
    .toggle-row { display: grid; gap: 10px; }
    .toggle { display: flex; align-items: center; gap: 10px; }
    .toggle input { width: 16px; height: 16px; accent-color: var(--green-dark); }
    .scope-meta {
      display: grid;
      gap: 8px;
      padding: 14px;
      border: 1px solid var(--line);
      background: rgba(21,25,15,.05);
    }
    .scope-meta span { color: var(--muted); font-size: 12px; }
    .case-list {
      margin: 0;
      padding-left: 18px;
      max-height: 220px;
      overflow: auto;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    /* Case ids are long, unbroken snake_case; without this they clip against
       the sidebar rather than wrapping. */
    .case-list li, .chip { overflow-wrap: anywhere; }
    .content { display: grid; gap: 20px; }
    .content-card { padding: 18px 18px 8px; }
    .table-wrap {
      overflow: auto;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.35);
      margin-top: 14px;
    }
    table { width: 100%; border-collapse: collapse; min-width: 1020px; }
    thead th {
      position: sticky;
      top: 0;
      background: var(--ink);
      color: var(--paper);
      text-transform: uppercase;
      letter-spacing: .08em;
      font-size: 10px;
      padding: 12px 14px;
      text-align: left;
    }
    tbody td {
      padding: 13px 14px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      font-size: 12.5px;
    }
    tbody tr:hover { background: rgba(183,219,82,.16); }
    tbody tr.is-production { background: rgba(46,74,37,.09); }
    .model-cell { display: grid; gap: 3px; }
    .model-cell strong { font-size: 13px; }
    .muted { color: var(--muted); }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      border: 1px solid var(--ink);
      background: var(--paper-2);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .04em;
    }
    .pill.good { background: rgba(46,74,37,.16); color: var(--good); }
    .pill.warn { background: rgba(229,110,54,.22); color: var(--warn); }
    .pill.bad { background: rgba(168,40,15,.16); color: var(--bad); }
    .pill.prod { background: var(--green); color: var(--ink); }
    .accuracy-stack { display: grid; gap: 8px; min-width: 130px; }
    .bar { width: 100%; height: 9px; border: 1px solid var(--ink); background: var(--paper-2); overflow: hidden; }
    .bar > span { display: block; height: 100%; }
    /* Trajectory panel. Collapsed by default: the path a case took is worth
       seeing on demand, not worth a column of its own on every row. */
    .expand-btn {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 2px 6px;
      border: 1px solid var(--line);
      background: var(--paper-2);
      color: inherit;
      font: inherit;
      font-size: 12.5px;
      cursor: pointer;
    }
    .expand-btn:hover { border-color: var(--ink); background: var(--green); }
    .expand-btn:focus-visible { outline: 2px solid var(--orange); outline-offset: 1px; }
    .expand-btn .caret { color: var(--green-dark); font-size: 10px; }
    tr.traj-row > td { background: rgba(21,25,15,.05); padding: 0 14px 16px; }
    .traj-panel { display: grid; gap: 14px; padding-top: 4px; }
    .traj-case { display: grid; gap: 6px; }
    .traj-head {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      font-weight: 900;
      letter-spacing: .04em;
      overflow-wrap: anywhere;
    }
    .traj-line { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
    .traj-count {
      min-width: 62px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .04em;
    }
    .node {
      padding: 3px 8px;
      border: 1px solid var(--ink);
      background: var(--paper);
      font-size: 11px;
      font-weight: 700;
    }
    .node.answer { background: var(--ink); color: var(--paper); border-color: var(--ink); }
    .node.tool { background: rgba(183,219,82,.4); }
    .arrow { color: var(--orange); font-size: 11px; }
    .traj-note { margin: 0; color: var(--muted); font-size: 11.5px; line-height: 1.6; }
    .case-chips { display: flex; flex-wrap: wrap; gap: 6px; max-width: 340px; }
    .chip {
      font-size: 11px;
      padding: 2px 6px;
      border: 1px solid var(--line);
      font-family: inherit;
    }
    .chip.pass { background: rgba(46,74,37,.14); color: var(--good); }
    .chip.flaky { background: rgba(229,110,54,.2); color: var(--warn); border-color: var(--orange); }
    .chip.fail { background: rgba(168,40,15,.14); color: var(--bad); border-color: var(--bad); }
    .chip.errored { background: rgba(21,25,15,.08); color: var(--muted); }
    .empty-state {
      padding: 64px 24px;
      text-align: center;
      background: rgba(255,255,255,.22);
      border: 2px solid var(--ink);
      box-shadow: var(--shadow);
    }
    .empty-state h2 { margin-top: 0; font-family: Georgia, serif; font-weight: 500; }
    .footnote { margin-top: 12px; color: var(--muted); font-size: 11.5px; line-height: 1.6; }
    @media (max-width: 1040px) {
      .layout { grid-template-columns: 1fr; }
      .sidebar { position: static; }
    }
    @media (max-width: 720px) {
      .shell { width: min(100% - 22px, 1480px); }
      .prod-grid { grid-template-columns: 1fr; }
    }
"""


_SCRIPT = """
const payload = JSON.parse(document.getElementById("leaderboard-data").textContent || "{}");
const app = document.getElementById("app");

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function scoreClass(value) {
  if (value >= 80) return "good";
  if (value >= 50) return "warn";
  return "bad";
}

function fmtPercent(value) {
  return value == null ? "—" : `${Number(value).toFixed(1)}%`;
}

function fmtSeconds(value) {
  return value == null ? "—" : `${Number(value).toFixed(2)}s`;
}

function fmtCost(value) {
  return value == null ? "—" : `$${Number(value).toFixed(4)}`;
}

// Mirrors display_reasoning_effort in scorecard.py: a production run records
// the literal "production" here because each feature used its own level.
function displayReasoning(value) {
  if (value === "production") return "as configured";
  return value || "none";
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function fmtToolCalls(value) {
  return value == null ? "—" : Number(value).toFixed(1);
}

// null covers two cases that must not read as zero: a feature with no tool
// loop, and a run recorded before trajectory was captured.
function trajectorySentence(row) {
  if (row.avg_tool_calls == null) {
    return "No tool loop — one call, no lookups";
  }
  const parts = [`${fmtToolCalls(row.avg_tool_calls)} lookups per attempt`];
  if (row.varied_path_count) {
    parts.push(`${plural(row.varied_path_count, "case")} took a different path on a rerun`);
  } else {
    parts.push("same path every time");
  }
  if (row.capped_case_count) {
    parts.push(`${plural(row.capped_case_count, "case")} hit the tool-call ceiling`);
  }
  return parts.join(" · ");
}

function productionCards() {
  return payload.production.map((entry) => {
    const row = entry.row;
    const head = `
      <h3>${escapeHtml(entry.feature)}</h3>
      ${entry.blurb ? `<p class="prod-blurb">${escapeHtml(entry.blurb)}</p>` : ""}
    `;
    if (!row) {
      return `
        <article class="prod-card missing">
          ${head}
          <p class="prod-score muted">Not measured yet</p>
          <div class="prod-meta">
            ${plural(entry.total_cases, "case")} written, no recorded run on the
            model this feature ships with.
          </div>
        </article>
      `;
    }
    const scoredAttempts = row.scored_attempts;
    const facts = [
      [
        "Strict",
        `${row.stable_pass_count} of ${plural(row.scored_case_count, "case")} passed
         all ${plural(row.repeat, "attempt")}`
      ],
      [
        "Attempt",
        `${row.passed} of ${plural(scoredAttempts, "attempt")} passed
         (${fmtPercent(row.accuracy)})`
      ],
      row.flaky_count
        ? ["Flaky", `${plural(row.flaky_count, "case")} passed sometimes, failed others`]
        : ["Flaky", "None — every case landed the same way each time"],
      ["Tools", trajectorySentence(row)],
      ["Route", escapeHtml(row.routes.join(", ") || "—")],
      [
        "Cost",
        `${fmtCost(row.cost_per_repeat)} per run · ${fmtSeconds(row.avg_latency_s)} per attempt`
      ],
      [
        "Recorded",
        `${escapeHtml(row.recorded_on)} · commit ${escapeHtml(row.revision_label)}`
      ]
    ];
    return `
      <article class="prod-card">
        ${head}
        <p class="prod-score">
          <span class="prod-headline ${scoreClass(row.strict_accuracy)}">${fmtPercent(row.strict_accuracy)}</span>
          <span class="muted">of cases passed every attempt</span>
        </p>
        <dl class="prod-facts">
          ${facts.map(([term, value]) => `<div><dt>${term}</dt><dd>${value}</dd></div>`).join("")}
        </dl>
      </article>
    `;
  }).join("");
}

function productionWarnings() {
  const notes = [];
  if (payload.uncovered_features.length) {
    notes.push([
      "bad",
      `Never measured on the model it ships with:
       <strong>${payload.uncovered_features.map(escapeHtml).join(", ")}</strong>.
       Whatever the other cards say, they say nothing about these.`
    ]);
  }
  for (const entry of payload.production) {
    const row = entry.row;
    if (!row || !row.missing_case_ids.length) continue;
    notes.push([
      "",
      `<strong>${escapeHtml(entry.feature)}</strong> was last measured before
       ${plural(row.missing_case_ids.length, "case")} existed, so its score does
       not cover:
       ${row.missing_case_ids.map((id) => `<code>${escapeHtml(id)}</code>`).join(", ")}`
    ]);
  }
  const fellBack = payload.production.filter(
    (entry) => entry.row && entry.row.fallback_case_ids.length
  );
  if (fellBack.length) {
    notes.push([
      "",
      `Answered by a fallback model:
       <strong>${fellBack.map((entry) => escapeHtml(entry.feature)).join(", ")}</strong>.
       The first-choice model failed on at least one case, so part of that score
       belongs to the fallback rather than the model named on the card.`
    ]);
  }
  const single = payload.production.filter((entry) => entry.row && entry.row.repeat < 2);
  if (single.length) {
    notes.push([
      "",
      `Measured once only:
       <strong>${single.map((entry) => escapeHtml(entry.feature)).join(", ")}</strong>.
       A single sample cannot tell a reliable pass from a lucky one — these need
       a rerun at three attempts or more.`
    ]);
  }
  if (!notes.length) return "";
  return `<div class="warnings">${notes
    .map(([kind, text]) => `<div class="warning ${kind}">${text}</div>`)
    .join("")}</div>`;
}

function pathNodes(path) {
  const nodes = path.map((name) => `<span class="node tool">${escapeHtml(name)}</span>`);
  nodes.push('<span class="node answer">reply</span>');
  return nodes.join('<span class="arrow">&rarr;</span>');
}

// Cases whose reruns agreed on a path are grouped by that path: eleven
// identical lines is noise, "9 cases: run_sql -> reply" is the finding.
function groupSteadyPaths(cases) {
  const byPath = new Map();
  for (const item of cases) {
    const entry = item.tool_path_counts[0];
    const key = entry.path.join("\u2192");
    if (!byPath.has(key)) byPath.set(key, { path: entry.path, ids: [] });
    byPath.get(key).ids.push(item.case_id);
  }
  return [...byPath.values()].sort((a, b) => b.ids.length - a.ids.length);
}

function hasTrajectory(row) {
  return row.case_rows.some((item) => item.tool_path_counts.length);
}

function trajectoryPanel(row) {
  const cases = row.case_rows.filter((item) => item.tool_path_counts.length);
  if (!cases.length) return "";
  const varied = cases.filter((item) => item.path_varied);
  const steady = cases.filter((item) => !item.path_varied);
  const blocks = varied.map((item) => `
    <div class="traj-case">
      <div class="traj-head">
        ${escapeHtml(item.case_id)}
        <span class="pill warn">${plural(item.tool_path_counts.length, "path")}</span>
        <span class="muted">${escapeHtml(item.rate_label)} passing</span>
      </div>
      ${item.tool_path_counts.map((entry) => `
        <div class="traj-line">
          <span class="traj-count">&times;${entry.count}</span>
          ${pathNodes(entry.path)}
        </div>
      `).join("")}
    </div>
  `).join("");
  const steadyBlock = steady.length
    ? `<div class="traj-case">
         <div class="traj-head">Same path on every attempt</div>
         ${groupSteadyPaths(steady).map((group) => `
           <div class="traj-line" title="${escapeHtml(group.ids.join(", "))}">
             <span class="traj-count">${plural(group.ids.length, "case")}</span>
             ${pathNodes(group.path)}
           </div>
         `).join("")}
       </div>`
    : "";
  const capped = row.capped_case_count
    ? `<p class="traj-note">${plural(row.capped_case_count, "case")} ran out of
       tool-call budget and was made to answer with what it had.</p>`
    : "";
  const hint = `<p class="traj-note">How each case reached its answer.
    <strong>&times;N</strong> is how many attempts took that exact route.</p>`;
  return `<div class="traj-panel">${hint}${blocks}${steadyBlock}${capped}</div>`;
}

// Hover detail for one case: what failed, and — when the reruns disagreed on
// how to get there — every distinct tool path it took.
function chipTitle(item) {
  const lines = [item.failure_names.join(", ")].filter(Boolean);
  if (item.path_varied && item.tool_paths.length) {
    lines.push(
      "Paths taken: " +
        item.tool_paths.map((path) => (path.length ? path.join(" → ") : "no tools")).join(" | ")
    );
  }
  return lines.join("\\n") || "no recorded failures";
}

function caseChips(row) {
  // A case that passes every attempt by a different route is still worth
  // seeing: the score is clean and the behaviour is not settled.
  const interesting = row.case_rows.filter(
    (item) => item.outcome !== "pass" || item.path_varied
  );
  if (!interesting.length) {
    return `<span class="chip pass">all ${row.case_rows.length} passing</span>`;
  }
  return interesting
    .map((item) => `<span class="chip ${item.outcome}" title="${escapeHtml(chipTitle(item))}">
        ${escapeHtml(item.case_id)} ${escapeHtml(item.rate_label)}${item.path_varied ? ` · ${item.tool_path_counts.length} paths` : ""}
      </span>`)
    .join("");
}

if (payload.features && payload.features.length) {
  const state = {
    feature: payload.features[0].feature,
    model: "all",
    reasoning: "all",
    sort: "strict_accuracy",
    flakyOnly: false,
    productionOnly: false,
    query: "",
    // Run ids whose trajectory panel is open. Kept in state so a filter or
    // sort change does not silently collapse what the reader was reading.
    expanded: new Set()
  };

  app.innerHTML = `
    <h2 class="section-title">What ships today</h2>
    <p class="section-sub">
      The most recent scored run for each feature, on the model it actually
      runs on, against the ${payload.total_cases} cases written so far.
      Anything the numbers do not cover is spelled out underneath them.
    </p>
    <div class="prod-grid">${productionCards()}</div>
    ${productionWarnings()}

    <h2 class="section-title">Model comparisons</h2>
    <p class="section-sub">
      The same cases, run on other models and reasoning levels — this is how a
      feature's model gets chosen, and it is why the fact-checker runs on a
      budget model that beat the premium ones. One feature at a time, since a
      model is only better or worse at a specific job.
    </p>
    <div class="layout">
      <aside class="sidebar">
        <h2>Filters</h2>
        <div class="filter-grid">
          <div class="field">
            <label for="feature-filter">Feature</label>
            <select id="feature-filter"></select>
          </div>
          <div class="field">
            <label for="model-filter">Model</label>
            <select id="model-filter"></select>
          </div>
          <div class="field">
            <label for="reasoning-filter">Reasoning</label>
            <select id="reasoning-filter"></select>
          </div>
          <div class="field">
            <label for="sort-filter">Sort Rows</label>
            <select id="sort-filter">
              <option value="strict_accuracy">Strict accuracy</option>
              <option value="accuracy">Attempt accuracy</option>
              <option value="flaky_count">Fewest flaky</option>
              <option value="cost_per_repeat">Cost per run</option>
              <option value="avg_latency_s">Avg latency</option>
              <option value="created_at">Newest</option>
            </select>
          </div>
          <div class="field">
            <label for="query-filter">Search</label>
            <input id="query-filter" type="search" placeholder="Model, revision, case…" />
          </div>
          <div class="toggle-row">
            <label class="toggle"><input id="flaky-only" type="checkbox" />Has flaky cases</label>
            <label class="toggle"><input id="production-only" type="checkbox" />What ships only</label>
          </div>
        </div>
        <div class="scope-meta">
          <strong id="scope-title"></strong>
          <span id="scope-blurb"></span>
          <span id="scope-meta"></span>
          <ul id="scope-cases" class="case-list"></ul>
        </div>
      </aside>
      <section class="content">
        <section class="content-card">
          <h2>Measured setups</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th title="The model asked, its reasoning level, and when it was run">Model</th>
                  <th title="Share of cases that passed every attempt">Strict</th>
                  <th title="Share of individual attempts that passed">Attempt</th>
                  <th title="How many times each case was run">Repeat</th>
                  <th title="Cases this run covered">Cases</th>
                  <th title="Cases that passed some attempts and failed others">Flaky</th>
                  <th title="Average lookups per attempt; varied = cases that took a different path on a rerun">Tool calls</th>
                  <th title="Average time for one attempt">Avg Latency</th>
                  <th title="Average spend to run the whole case set once">Cost/run</th>
                  <th title="Commit the run was recorded at">Recorded</th>
                  <th title="Cases that did not pass every attempt, with their pass rate">Not passing</th>
                </tr>
              </thead>
              <tbody id="results-body"></tbody>
            </table>
          </div>
          <div class="footnote">
            One row per model, reasoning level, route and repeat count — the newest of
            each. Repeat is part of a row's identity: a five-sample run and a
            one-sample run of the same model are different measurements, so they are
            never merged.
          </div>
        </section>
      </section>
    </div>
  `;

  const els = {
    feature: document.getElementById("feature-filter"),
    model: document.getElementById("model-filter"),
    reasoning: document.getElementById("reasoning-filter"),
    sort: document.getElementById("sort-filter"),
    flakyOnly: document.getElementById("flaky-only"),
    productionOnly: document.getElementById("production-only"),
    query: document.getElementById("query-filter"),
    scopeTitle: document.getElementById("scope-title"),
    scopeBlurb: document.getElementById("scope-blurb"),
    scopeMeta: document.getElementById("scope-meta"),
    scopeCases: document.getElementById("scope-cases"),
    body: document.getElementById("results-body")
  };

  function getSection() {
    return payload.features.find((section) => section.feature === state.feature)
      || payload.features[0];
  }

  function buildOptions(select, values, current) {
    select.innerHTML = "";
    for (const option of values) {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      node.selected = option.value === current;
      select.appendChild(node);
    }
  }

  function sortRows(rows) {
    const sorted = [...rows];
    if (state.sort === "created_at") {
      sorted.sort((a, b) => b.created_at.localeCompare(a.created_at));
      return sorted;
    }
    if (state.sort === "strict_accuracy" || state.sort === "accuracy") {
      sorted.sort((a, b) => (b[state.sort] ?? -1) - (a[state.sort] ?? -1));
      return sorted;
    }
    sorted.sort((a, b) => (a[state.sort] ?? Infinity) - (b[state.sort] ?? Infinity));
    return sorted;
  }

  function applyFilters(section) {
    const query = state.query.trim().toLowerCase();
    let rows = section.rows;
    rows = rows.filter((row) => state.model === "all" || row.model_display === state.model);
    rows = rows.filter((row) => state.reasoning === "all" || (row.reasoning_effort || "none") === state.reasoning);
    rows = rows.filter((row) => !state.flakyOnly || row.flaky_count > 0);
    rows = rows.filter((row) => !state.productionOnly || row.is_production);
    rows = rows.filter((row) => {
      if (!query) return true;
      const haystack = [
        row.model_display,
        row.reasoning_effort || "none",
        row.routes.join(" "),
        row.git_sha_short,
        row.case_rows.filter((item) => item.outcome !== "pass").map((item) => item.case_id).join(" ")
      ].join(" ").toLowerCase();
      return haystack.includes(query);
    });
    return sortRows(rows);
  }

  function renderTable(rows) {
    els.body.innerHTML = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="11" class="muted">No runs match the current filters.</td>`;
      els.body.appendChild(tr);
      return;
    }
    for (const row of rows) {
      const tr = document.createElement("tr");
      if (row.is_production) tr.className = "is-production";
      const strict = Number(row.strict_accuracy);
      const open = state.expanded.has(row.run_id);
      tr.innerHTML = `
        <td>
          <div class="model-cell">
            <strong>${escapeHtml(row.model_display)}</strong>
            <span class="muted">
              ${escapeHtml(displayReasoning(row.reasoning_effort))}
              · ${escapeHtml(row.created_at.slice(0, 16).replace("T", " "))}
              ${row.is_production ? '<span class="pill prod">production</span>' : ""}
            </span>
            <span class="muted">${escapeHtml(row.routes.join(", "))}</span>
          </div>
        </td>
        <td>
          <div class="accuracy-stack">
            <span class="pill ${scoreClass(strict)}">${fmtPercent(strict)}</span>
            <div class="bar"><span style="width:${Math.max(0, Math.min(100, strict))}%;
              background:${strict >= 80 ? "var(--good)" : strict >= 50 ? "var(--warn)" : "var(--bad)"}"></span></div>
          </div>
        </td>
        <td>${fmtPercent(row.accuracy)}</td>
        <td>${row.repeat}</td>
        <td>${row.case_ids.length}</td>
        <td>${row.flaky_count || "—"}</td>
        <td>
          ${hasTrajectory(row)
            ? `<button class="expand-btn" type="button" data-run="${escapeHtml(row.run_id)}"
                 aria-expanded="${open}" title="Show the tool path each case took">
                 ${fmtToolCalls(row.avg_tool_calls)}
                 <span class="caret">${open ? "▾" : "▸"}</span>
               </button>`
            : fmtToolCalls(row.avg_tool_calls)}
          ${row.varied_path_count ? `<br><span class="muted">${row.varied_path_count} varied</span>` : ""}
          ${row.capped_case_count ? `<br><span class="muted">${row.capped_case_count} capped</span>` : ""}
        </td>
        <td>${fmtSeconds(row.avg_latency_s)}</td>
        <td>${fmtCost(row.cost_per_repeat)}</td>
        <td class="muted">${escapeHtml(row.recorded_on)}<br>${escapeHtml(row.revision_label)}</td>
        <td><div class="case-chips">${caseChips(row)}</div></td>
      `;
      els.body.appendChild(tr);
      if (open) {
        const detail = document.createElement("tr");
        detail.className = "traj-row";
        detail.innerHTML = `<td colspan="11">${trajectoryPanel(row)}</td>`;
        els.body.appendChild(detail);
      }
    }
  }

  function renderScopeMeta(section) {
    els.scopeTitle.textContent = `${section.feature} · ${section.total_cases} cases`;
    els.scopeBlurb.textContent = section.blurb || "";
    els.scopeMeta.textContent = `${plural(section.rows.length, "measured setup")} below. Every one was scored on these cases:`;
    els.scopeCases.innerHTML = "";
    for (const caseId of section.case_ids) {
      const li = document.createElement("li");
      li.textContent = caseId;
      els.scopeCases.appendChild(li);
    }
  }

  function refreshDynamicOptions() {
    const section = getSection();
    const models = Array.from(new Set(section.rows.map((row) => row.model_display))).sort();
    const reasoning = Array.from(new Set(section.rows.map((row) => row.reasoning_effort || "none"))).sort();
    buildOptions(els.model, [{ value: "all", label: "All models" }, ...models.map((value) => ({ value, label: value }))], state.model);
    buildOptions(els.reasoning, [{ value: "all", label: "All reasoning levels" }, ...reasoning.map((value) => ({ value, label: displayReasoning(value) }))], state.reasoning);
  }

  function render() {
    const section = getSection();
    renderScopeMeta(section);
    renderTable(applyFilters(section));
  }

  buildOptions(
    els.feature,
    payload.features.map((section) => ({
      value: section.feature,
      label: `${section.feature} (${section.total_cases} cases)`
    })),
    state.feature
  );

  els.feature.addEventListener("change", (event) => {
    state.feature = event.target.value;
    state.model = "all";
    state.reasoning = "all";
    refreshDynamicOptions();
    render();
  });
  els.model.addEventListener("change", (event) => { state.model = event.target.value; render(); });
  els.reasoning.addEventListener("change", (event) => { state.reasoning = event.target.value; render(); });
  els.sort.addEventListener("change", (event) => { state.sort = event.target.value; render(); });
  els.flakyOnly.addEventListener("change", (event) => { state.flakyOnly = event.target.checked; render(); });
  els.productionOnly.addEventListener("change", (event) => { state.productionOnly = event.target.checked; render(); });
  els.query.addEventListener("input", (event) => { state.query = event.target.value; render(); });
  els.body.addEventListener("click", (event) => {
    const button = event.target.closest(".expand-btn");
    if (!button) return;
    const runId = button.dataset.run;
    if (state.expanded.has(runId)) state.expanded.delete(runId);
    else state.expanded.add(runId);
    render();
  });
  refreshDynamicOptions();
  render();
} else if (app) {
  app.innerHTML = `
    <h2 class="section-title">Production</h2>
    <div class="prod-grid">${productionCards()}</div>
    ${productionWarnings()}
  `;
}
"""
