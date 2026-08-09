"""Interactive HTML rendering for the eval leaderboard."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.framework import EvalCase
from evals.leaderboard.record import HTML_PATH
from evals.leaderboard.scorecard import build_scorecard

_TITLE = "Feedback Eval Leaderboard"
_NOTE = (
    "The production table is what the daemon ships today. Everything below it "
    "is an alternative measured against that baseline."
)
_GITHUB_REPO = "https://github.com/Alchemication/zdrowskit"

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


def render_leaderboard_html(
    runs: list[dict[str, Any]],
    *,
    inventory: list[EvalCase] | None = None,
    head_sha: str | None = None,
    stale_check: Callable[[str], bool | None] | None = None,
    nav_base: str | None = None,
) -> str:
    """Render an interactive HTML leaderboard from persisted run records.

    Args:
        runs: Persisted run records.
        inventory: Current eval cases, used to flag runs recorded before a case
            existed.
        head_sha: Commit to diff recorded revisions against for staleness.
        stale_check: Override for the staleness resolver.
        nav_base: Relative path to the site root, which adds the shared site
            header and footer. Omit for the standalone local artifact, whose
            sibling `docs/` holds Markdown rather than the built pages.

    Returns:
        A complete, self-contained HTML document.
    """
    payload = build_scorecard(
        runs, inventory=inventory, head_sha=head_sha, stale_check=stale_check
    )
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
  <meta name="description" content="Feedback-derived regression scores for every zdrowskit LLM surface, published in full.">
  <style>{_load_base_css()}{_STYLE}</style>
</head>
<body>
{header}  <main class="page shell">
    <section class="hero">
      <div class="eyebrow">Eval Leaderboard</div>
      <h1>{_TITLE}</h1>
      <div class="lede">{_NOTE}</div>
    </section>
    {app}
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
    stale_check: Callable[[str], bool | None] | None = None,
    nav_base: str | None = None,
) -> str:
    """Write the rendered leaderboard HTML to disk.

    Args:
        runs: Persisted run records.
        html_path: Destination path; defaults to the repo's `HTML_PATH`.
        inventory: Current eval cases.
        head_sha: Commit to diff recorded revisions against for staleness.
        stale_check: Override for the staleness resolver.
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
        stale_check=stale_check,
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
    .prod-score { margin: 0; display: flex; flex-wrap: wrap; gap: 6px; }
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

function productionCards() {
  return payload.production.map((entry) => {
    const row = entry.row;
    if (!row) {
      return `
        <article class="prod-card missing">
          <h3>${escapeHtml(entry.feature)}</h3>
          <p class="prod-score muted">—</p>
          <div class="prod-meta">
            Never recorded on production routes.<br>
            ${entry.total_cases} case(s) waiting.
          </div>
        </article>
      `;
    }
    const flaky = row.flaky_count
      ? `<span class="pill warn">${row.flaky_count} flaky</span>`
      : "";
    return `
      <article class="prod-card">
        <h3>${escapeHtml(entry.feature)}</h3>
        <p class="prod-score">
          <span class="pill ${scoreClass(row.strict_accuracy)}">${fmtPercent(row.strict_accuracy)}</span>
          ${flaky}
        </p>
        <div class="prod-meta">
          ${escapeHtml(row.routes.join(", ") || "—")}<br>
          ${row.case_ids.length}/${entry.total_cases} cases · repeat ${row.repeat}
          · attempt ${fmtPercent(row.accuracy)}<br>
          ${fmtSeconds(row.avg_latency_s)} · ${fmtCost(row.cost_per_repeat)}/run
          · ${escapeHtml(row.revision_label)}${row.stale ? " (stale)" : ""}
        </div>
      </article>
    `;
  }).join("");
}

function productionWarnings() {
  const notes = [];
  if (payload.uncovered_features.length) {
    notes.push([
      "bad",
      `No production run recorded for <strong>${payload.uncovered_features.map(escapeHtml).join(", ")}</strong>.
       A green scorecard says nothing about these features.`
    ]);
  }
  for (const entry of payload.production) {
    const row = entry.row;
    if (!row || !row.missing_case_ids.length) continue;
    notes.push([
      "",
      `<strong>${escapeHtml(entry.feature)}</strong> was recorded before
       ${row.missing_case_ids.length} current case(s) existed:
       ${row.missing_case_ids.map((id) => `<code>${escapeHtml(id)}</code>`).join(", ")}`
    ]);
  }
  const stale = payload.production.filter((entry) => entry.row && entry.row.stale);
  if (stale.length) {
    notes.push([
      "",
      `Measured code that has since changed:
       <strong>${stale.map((entry) => escapeHtml(entry.feature)).join(", ")}</strong>.
       Re-record to score the code as it stands.`
    ]);
  }
  const single = payload.production.filter((entry) => entry.row && entry.row.repeat < 2);
  if (single.length) {
    notes.push([
      "",
      `Single sample (repeat=1):
       <strong>${single.map((entry) => escapeHtml(entry.feature)).join(", ")}</strong>.
       Stability is unmeasured — rerun with <code>--repeat 3</code> or more.`
    ]);
  }
  if (!notes.length) return "";
  return `<div class="warnings">${notes
    .map(([kind, text]) => `<div class="warning ${kind}">${text}</div>`)
    .join("")}</div>`;
}

function caseChips(row) {
  const interesting = row.case_rows.filter((item) => item.outcome !== "pass");
  if (!interesting.length) {
    return `<span class="chip pass">all ${row.case_rows.length} passing</span>`;
  }
  return interesting
    .map((item) => `<span class="chip ${item.outcome}" title="${escapeHtml(item.failure_names.join(", "))}">
        ${escapeHtml(item.case_id)} ${escapeHtml(item.rate_label)}
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
    hideStale: false,
    query: ""
  };

  app.innerHTML = `
    <h2 class="section-title">Production</h2>
    <p class="section-sub">
      Latest run on production routes per feature, against the
      ${payload.total_cases} cases in <code>evals/cases</code> today.
    </p>
    <div class="prod-grid">${productionCards()}</div>
    ${productionWarnings()}

    <h2 class="section-title">Variations</h2>
    <p class="section-sub">
      Alternatives for one feature at a time. Strict counts cases passing every
      attempt; attempt is attempt-weighted. They diverge exactly when cases are flaky.
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
            <label class="toggle"><input id="production-only" type="checkbox" />Production routes only</label>
            <label class="toggle"><input id="hide-stale" type="checkbox" />Current commit only</label>
          </div>
        </div>
        <div class="scope-meta">
          <strong id="scope-title"></strong>
          <span id="scope-meta"></span>
          <ul id="scope-cases" class="case-list"></ul>
        </div>
      </aside>
      <section class="content">
        <section class="content-card">
          <h2>Runs</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Strict</th>
                  <th>Attempt</th>
                  <th>Repeat</th>
                  <th>Cases</th>
                  <th>Flaky</th>
                  <th>Avg Latency</th>
                  <th>Cost/run</th>
                  <th>Revision</th>
                  <th>Not passing</th>
                </tr>
              </thead>
              <tbody id="results-body"></tbody>
            </table>
          </div>
          <div class="footnote">
            One row per model, reasoning, route and repeat setting — the newest of each.
            Repeat is part of the identity: a 5-sample row and a 1-sample row are
            different measurements and are never merged.
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
    hideStale: document.getElementById("hide-stale"),
    query: document.getElementById("query-filter"),
    scopeTitle: document.getElementById("scope-title"),
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
    rows = rows.filter((row) => !state.hideStale || !row.stale);
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
      tr.innerHTML = `<td colspan="10" class="muted">No runs match the current filters.</td>`;
      els.body.appendChild(tr);
      return;
    }
    for (const row of rows) {
      const tr = document.createElement("tr");
      if (row.is_production) tr.className = "is-production";
      const strict = Number(row.strict_accuracy);
      tr.innerHTML = `
        <td>
          <div class="model-cell">
            <strong>${escapeHtml(row.model_display)}</strong>
            <span class="muted">
              ${escapeHtml(row.reasoning_effort || "none")}
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
        <td>${fmtSeconds(row.avg_latency_s)}</td>
        <td>${fmtCost(row.cost_per_repeat)}</td>
        <td><span class="pill">${escapeHtml(row.revision_label)}${row.stale ? " stale" : ""}</span></td>
        <td><div class="case-chips">${caseChips(row)}</div></td>
      `;
      els.body.appendChild(tr);
    }
  }

  function renderScopeMeta(section) {
    els.scopeTitle.textContent = `${section.feature} · ${section.total_cases} cases`;
    els.scopeMeta.textContent = `${section.rows.length} recorded variation(s)`;
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
    buildOptions(els.reasoning, [{ value: "all", label: "All reasoning levels" }, ...reasoning.map((value) => ({ value, label: value }))], state.reasoning);
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
  els.hideStale.addEventListener("change", (event) => { state.hideStale = event.target.checked; render(); });
  els.query.addEventListener("input", (event) => { state.query = event.target.value; render(); });
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
