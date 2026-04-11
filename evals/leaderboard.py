"""Leaderboard recording and rendering for feedback-driven evals."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.framework import EvalResult

LEADERBOARD_DIR = Path(__file__).resolve().parent / "leaderboard"
RUNS_PATH = LEADERBOARD_DIR / "runs.jsonl"
MARKDOWN_PATH = Path(__file__).resolve().parent / "leaderboard.md"
HTML_PATH = Path(__file__).resolve().parent / "leaderboard.html"
_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RecordRunOutcome:
    """Result of attempting to persist an eval leaderboard run."""

    recorded: bool
    record: dict[str, Any]
    duplicate_of: str | None = None


def compute_case_set_id(case_ids: list[str]) -> str:
    """Return a stable fingerprint for a sorted set of case ids."""
    return _stable_hash(sorted(case_ids))


def compute_run_fingerprint(
    *,
    git_sha: str,
    case_set_id: str,
    model: str,
    reasoning_effort: str | None,
    max_tool_iterations: int,
) -> str:
    """Return a stable fingerprint for one comparable eval run."""
    return _stable_hash(
        {
            "git_sha": git_sha,
            "case_set_id": case_set_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "max_tool_iterations": max_tool_iterations,
        }
    )


def get_repo_context(repo_root: Path = _ROOT) -> dict[str, Any]:
    """Return the current git sha and dirty state for the repository."""
    git_sha = _git_output(["git", "rev-parse", "HEAD"], cwd=repo_root) or "unknown"
    dirty = bool(_git_output(["git", "status", "--porcelain"], cwd=repo_root))
    return {"git_sha": git_sha, "dirty": dirty}


def build_run_record(
    *,
    results: list[EvalResult],
    case_ids: list[str],
    model: str,
    reasoning_effort: str | None,
    max_tool_iterations: int,
    feature_filter: str | None,
    repo_context: dict[str, Any] | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build a normalized leaderboard record from eval results."""
    sorted_case_ids = sorted(case_ids)
    context = repo_context or get_repo_context()
    case_set_id = compute_case_set_id(sorted_case_ids)
    summary = _build_summary_metrics(results)
    record = {
        "run_id": run_id or uuid.uuid4().hex,
        "created_at": created_at or _utc_now_iso(),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "max_tool_iterations": max_tool_iterations,
        "case_ids": sorted_case_ids,
        "case_count": len(sorted_case_ids),
        "feature_filter": feature_filter,
        "case_set_id": case_set_id,
        "git_sha": str(context.get("git_sha", "unknown")),
        "dirty": bool(context.get("dirty", False)),
        "summary": summary,
        "per_case": [_build_case_result(result) for result in results],
    }
    record["run_fingerprint"] = compute_run_fingerprint(
        git_sha=record["git_sha"],
        case_set_id=record["case_set_id"],
        model=model,
        reasoning_effort=reasoning_effort,
        max_tool_iterations=max_tool_iterations,
    )
    return record


def load_run_records(runs_path: Path | None = None) -> list[dict[str, Any]]:
    """Load all persisted leaderboard run records from JSONL."""
    path = runs_path or RUNS_PATH
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        records.append(json.loads(stripped))
    return records


def render_leaderboard_markdown(runs: list[dict[str, Any]]) -> str:
    """Render leaderboard markdown from persisted run records."""
    lines = [
        "# Feedback Eval Leaderboard",
        "",
        "Feedback-derived regression scorecard for zdrowskit evals. "
        "Sections compare only runs over the same recorded case set; this is "
        "not a general benchmark.",
        "",
    ]
    if not runs:
        lines.extend(
            [
                "No recorded eval runs yet.",
                "",
                "Use `uv run python -m evals.run --record` to add the first run.",
                "",
            ]
        )
        return "\n".join(lines)

    for section in _build_sections(runs):
        feature_filter = section.get("feature_filter") or "all"
        case_ids = [f"`{case_id}`" for case_id in section.get("case_ids", [])]
        lines.extend(
            [
                f"## {section['case_count']} cases · feature={feature_filter} · case set `{section['case_set_id'][:12]}`",
                "",
                f"Latest recorded: `{section['latest_created_at']}`",
                "",
                "Case IDs: " + (", ".join(case_ids) if case_ids else "-"),
                "",
                "| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for row in section["rows"]:
            summary = row["summary"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        row["model"].split("/")[-1],
                        _display_reasoning_effort(row.get("reasoning_effort")),
                        _format_percent(float(summary["accuracy"])),
                        str(summary["passed"]),
                        str(summary["failed"]),
                        _format_optional_seconds(summary.get("avg_latency_s")),
                        _format_optional_seconds(summary.get("p95_latency_s")),
                        _format_optional_cost(summary.get("total_cost")),
                        _format_optional_cost(summary.get("avg_cost")),
                        _format_revision(row),
                        _format_failed_cases(row),
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


def write_leaderboard_markdown(
    runs: list[dict[str, Any]],
    markdown_path: Path | None = None,
) -> str:
    """Write the rendered leaderboard markdown to disk."""
    path = markdown_path or MARKDOWN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_leaderboard_markdown(runs)
    path.write_text(content, encoding="utf-8")
    return content


def render_leaderboard_html(runs: list[dict[str, Any]]) -> str:
    """Render an interactive HTML leaderboard from persisted run records."""
    payload = _build_html_payload(runs)
    title = "Feedback Eval Leaderboard"
    note = (
        "Feedback-derived regression scorecard for zdrowskit evals. "
        "Sections compare only runs over the same recorded case set; this is not "
        "a general benchmark."
    )
    empty = (
        '<div class="empty-state">'
        "<h2>No recorded eval runs yet</h2>"
        "<p>Use <code>uv run python -m evals.run --record</code> to add the first run.</p>"
        "</div>"
    )
    app = '<div id="app"></div>' if runs else empty
    data_json = json.dumps(payload, sort_keys=True).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f5f1e8;
      --panel: #fffdf8;
      --panel-2: #f1e9da;
      --ink: #1d2228;
      --muted: #6a6f75;
      --line: #d8ccba;
      --accent: #0f766e;
      --accent-soft: #d6efec;
      --good: #2f855a;
      --warn: #b7791f;
      --bad: #c53030;
      --shadow: 0 18px 40px rgba(36, 33, 29, 0.08);
      --radius: 20px;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      width: 100%;
      overflow-x: clip;
    }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(15, 118, 110, 0.10), transparent 32%),
        linear-gradient(180deg, #faf7f1, var(--bg));
    }}
    code {{
      font-family: "SFMono-Regular", "Menlo", monospace;
      background: #f3efe6;
      padding: 0.12rem 0.32rem;
      border-radius: 0.4rem;
    }}
    .page {{
      width: min(1480px, 100%);
      max-width: 1480px;
      margin: 0 auto;
      padding: clamp(24px, 3vw, 40px) clamp(16px, 2vw, 24px) 56px;
    }}
    .hero {{
      display: grid;
      gap: 12px;
      margin-bottom: 26px;
    }}
    .eyebrow {{
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 0.78rem;
      font-weight: 700;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(2rem, 3.7vw, 3.6rem);
      line-height: 0.95;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-weight: 700;
    }}
    .lede {{
      max-width: 900px;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.6;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(260px, 300px) minmax(0, 1fr);
      gap: 24px;
      align-items: start;
      min-width: 0;
    }}
    .sidebar, .content-card {{
      background: color-mix(in srgb, var(--panel) 86%, white);
      border: 1px solid rgba(216, 204, 186, 0.9);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }}
    .sidebar, .content, .content-card, .table-wrap {{
      min-width: 0;
    }}
    .sidebar {{
      position: sticky;
      top: 18px;
      padding: 20px;
      display: grid;
      gap: 18px;
    }}
    .sidebar h2, .content-card h2 {{
      margin: 0;
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .filter-grid {{
      display: grid;
      gap: 14px;
    }}
    .field {{
      display: grid;
      gap: 6px;
    }}
    .field label, .toggle {{
      font-size: 0.86rem;
      color: var(--muted);
      font-weight: 600;
    }}
    select, input[type="search"] {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: white;
      color: var(--ink);
      padding: 12px 14px;
      font: inherit;
    }}
    .toggle-row {{
      display: grid;
      gap: 10px;
    }}
    .toggle {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .toggle input {{
      width: 18px;
      height: 18px;
      accent-color: var(--accent);
    }}
    .scope-meta {{
      display: grid;
      gap: 8px;
      padding: 14px;
      border-radius: 16px;
      background: var(--panel-2);
      border: 1px solid rgba(216, 204, 186, 0.9);
    }}
    .scope-meta strong {{
      font-size: 1rem;
    }}
    .scope-meta span {{
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .case-list {{
      margin: 0;
      padding-left: 18px;
      max-height: 220px;
      overflow: auto;
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.45;
    }}
    .content {{
      display: grid;
      gap: 20px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
    }}
    .metric-card {{
      background: var(--panel);
      border: 1px solid rgba(216, 204, 186, 0.9);
      border-radius: 18px;
      padding: 18px;
      box-shadow: var(--shadow);
    }}
    .metric-card h3 {{
      margin: 0 0 8px;
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .metric-value {{
      margin: 0;
      font-size: clamp(1.5rem, 2.6vw, 2.2rem);
      font-weight: 700;
    }}
    .metric-sub {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .content-card {{
      padding: 18px 18px 8px;
    }}
    .table-wrap {{
      overflow: auto;
      border-radius: 18px;
      border: 1px solid rgba(216, 204, 186, 0.8);
      background: white;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 960px;
    }}
    thead th {{
      position: sticky;
      top: 0;
      background: #f7f1e7;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-size: 0.74rem;
      padding: 14px 16px;
      text-align: left;
      border-bottom: 1px solid var(--line);
    }}
    tbody td {{
      padding: 14px 16px;
      border-bottom: 1px solid rgba(216, 204, 186, 0.55);
      vertical-align: top;
      font-size: 0.94rem;
    }}
    tbody tr:hover {{
      background: rgba(15, 118, 110, 0.04);
    }}
    .model-cell {{
      display: grid;
      gap: 2px;
    }}
    .model-cell strong {{
      font-size: 0.98rem;
    }}
    .muted {{
      color: var(--muted);
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      padding: 0.22rem 0.62rem;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 700;
      background: var(--panel-2);
      color: var(--ink);
      border: 1px solid rgba(216, 204, 186, 0.9);
    }}
    .pill.good {{ background: rgba(47, 133, 90, 0.12); color: var(--good); border-color: rgba(47, 133, 90, 0.22); }}
    .pill.warn {{ background: rgba(183, 121, 31, 0.12); color: var(--warn); border-color: rgba(183, 121, 31, 0.22); }}
    .pill.bad {{ background: rgba(197, 48, 48, 0.12); color: var(--bad); border-color: rgba(197, 48, 48, 0.2); }}
    .accuracy-stack {{
      display: grid;
      gap: 8px;
      min-width: 130px;
    }}
    .bar {{
      width: 100%;
      height: 10px;
      border-radius: 999px;
      background: #eee5d7;
      overflow: hidden;
    }}
    .bar > span {{
      display: block;
      height: 100%;
      border-radius: inherit;
    }}
    .failed-cases {{
      max-width: 240px;
      white-space: normal;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }}
    .empty-state {{
      padding: 64px 24px;
      text-align: center;
      background: color-mix(in srgb, var(--panel) 88%, white);
      border: 1px solid rgba(216, 204, 186, 0.9);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }}
    .empty-state h2 {{
      margin-top: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
    }}
    .footnote {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 0.83rem;
    }}
    @media (max-width: 1040px) {{
      .layout {{
        grid-template-columns: 1fr;
      }}
      .sidebar {{
        position: static;
      }}
    }}
    @media (max-width: 720px) {{
      .page {{
        padding: 24px 16px 40px;
      }}
      .metrics {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="eyebrow">Eval Leaderboard</div>
      <h1>{title}</h1>
      <div class="lede">{note}</div>
    </section>
    {app}
  </main>
  <script id="leaderboard-data" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("leaderboard-data").textContent || "{{}}");
    if (payload.sections && payload.sections.length) {{
      const app = document.getElementById("app");
      const state = {{
        scopeId: payload.sections[0].case_set_id,
        model: "all",
        reasoning: "all",
        sort: "accuracy",
        latestOnly: true,
        failedOnly: false,
        dirtyOnly: false,
        query: ""
      }};
      app.innerHTML = `
        <div class="layout">
          <aside class="sidebar">
            <h2>Filters</h2>
            <div class="filter-grid">
              <div class="field">
                <label for="scope-filter">Scope</label>
                <select id="scope-filter"></select>
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
                  <option value="accuracy">Accuracy</option>
                  <option value="avg_cost">Avg Cost</option>
                  <option value="avg_latency_s">Avg Latency</option>
                  <option value="created_at">Newest</option>
                </select>
              </div>
              <div class="field">
                <label for="query-filter">Search</label>
                <input id="query-filter" type="search" placeholder="Model, revision, failed case…" />
              </div>
              <div class="toggle-row">
                <label class="toggle"><input id="latest-only" type="checkbox" checked />Latest row per model+reasoning</label>
                <label class="toggle"><input id="failed-only" type="checkbox" />Failures only</label>
                <label class="toggle"><input id="dirty-only" type="checkbox" />Dirty revisions only</label>
              </div>
            </div>
            <div class="scope-meta">
              <strong id="scope-title"></strong>
              <span id="scope-meta"></span>
              <ul id="scope-cases" class="case-list"></ul>
            </div>
          </aside>
          <section class="content">
            <div id="metric-grid" class="metrics"></div>
            <section class="content-card">
              <h2>Filtered Runs</h2>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Run</th>
                      <th>Accuracy</th>
                      <th>Passed</th>
                      <th>Failed</th>
                      <th>Avg Latency</th>
                      <th>p95 Latency</th>
                      <th>Total Cost</th>
                      <th>Avg Cost</th>
                      <th>Revision</th>
                      <th>Failed Cases</th>
                    </tr>
                  </thead>
                  <tbody id="results-body"></tbody>
                </table>
              </div>
              <div class="footnote">
                Latest-only mode is on by default so repeated reruns do not drown model comparisons.
              </div>
            </section>
          </section>
        </div>
      `;

      const els = {{
        scope: document.getElementById("scope-filter"),
        model: document.getElementById("model-filter"),
        reasoning: document.getElementById("reasoning-filter"),
        sort: document.getElementById("sort-filter"),
        latestOnly: document.getElementById("latest-only"),
        failedOnly: document.getElementById("failed-only"),
        dirtyOnly: document.getElementById("dirty-only"),
        query: document.getElementById("query-filter"),
        scopeTitle: document.getElementById("scope-title"),
        scopeMeta: document.getElementById("scope-meta"),
        scopeCases: document.getElementById("scope-cases"),
        metricGrid: document.getElementById("metric-grid"),
        body: document.getElementById("results-body")
      }};

      function accuracyClass(value) {{
        if (value >= 80) return "good";
        if (value >= 50) return "warn";
        return "bad";
      }}

      function fmtSeconds(value) {{
        return value == null ? "—" : `${{Number(value).toFixed(2)}}s`;
      }}

      function fmtCost(value) {{
        return value == null ? "—" : `$${{Number(value).toFixed(4)}}`;
      }}

      function getSection() {{
        return payload.sections.find((section) => section.case_set_id === state.scopeId) || payload.sections[0];
      }}

      function scopeOptionLabel(section) {{
        const feature = section.feature_filter || "all";
        return `${{section.case_count}} cases · ${{feature}} · ${{section.case_set_id.slice(0, 10)}}`;
      }}

      function buildOptions(select, values, current) {{
        select.innerHTML = "";
        for (const option of values) {{
          const node = document.createElement("option");
          node.value = option.value;
          node.textContent = option.label;
          node.selected = option.value === current;
          select.appendChild(node);
        }}
      }}

      function sortRows(rows) {{
        const sorted = [...rows];
        if (state.sort === "created_at") {{
          sorted.sort((a, b) => b.created_at.localeCompare(a.created_at));
          return sorted;
        }}
        sorted.sort((a, b) => {{
          const av = a.summary[state.sort];
          const bv = b.summary[state.sort];
          if (state.sort === "accuracy") return (bv ?? -1) - (av ?? -1);
          return (av ?? Number.POSITIVE_INFINITY) - (bv ?? Number.POSITIVE_INFINITY);
        }});
        return sorted;
      }}

      function latestOnly(rows) {{
        if (!state.latestOnly) return rows;
        const seen = new Set();
        const kept = [];
        for (const row of [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at))) {{
          const key = `${{row.model}}::${{row.reasoning_effort || "none"}}`;
          if (seen.has(key)) continue;
          seen.add(key);
          kept.push(row);
        }}
        return kept;
      }}

      function applyFilters(section) {{
        const query = state.query.trim().toLowerCase();
        let rows = latestOnly(section.runs);
        rows = rows.filter((row) => state.model === "all" || row.model === state.model);
        rows = rows.filter((row) => state.reasoning === "all" || (row.reasoning_effort || "none") === state.reasoning);
        rows = rows.filter((row) => !state.failedOnly || row.summary.failed > 0);
        rows = rows.filter((row) => !state.dirtyOnly || row.dirty);
        rows = rows.filter((row) => {{
          if (!query) return true;
          const haystack = [
            row.model,
            row.model_display,
            row.reasoning_effort || "none",
            row.git_sha_short,
            row.failed_cases.join(" "),
          ].join(" ").toLowerCase();
          return haystack.includes(query);
        }});
        return sortRows(rows);
      }}

      function renderSummary(rows) {{
        const accuracies = rows.map((row) => row.summary.accuracy);
        const costs = rows.map((row) => row.summary.avg_cost).filter((value) => value != null);
        const best = accuracies.length ? Math.max(...accuracies) : null;
        const avg = accuracies.length ? accuracies.reduce((sum, value) => sum + value, 0) / accuracies.length : null;
        const avgCost = costs.length ? costs.reduce((sum, value) => sum + value, 0) / costs.length : null;
        const failing = rows.filter((row) => row.summary.failed > 0).length;
        const metrics = [
          ["Visible Runs", String(rows.length), "Current rows after filters"],
          ["Best Accuracy", best == null ? "—" : `${{best.toFixed(1)}}%`, "Strongest visible run"],
          ["Average Accuracy", avg == null ? "—" : `${{avg.toFixed(1)}}%`, "Across filtered rows"],
          ["Failing Rows", String(failing), avgCost == null ? "No avg cost yet" : `Avg row cost $${{avgCost.toFixed(4)}}`],
        ];
        els.metricGrid.innerHTML = "";
        for (const [label, value, sub] of metrics) {{
          const card = document.createElement("article");
          card.className = "metric-card";
          card.innerHTML = `<h3>${{label}}</h3><p class="metric-value">${{value}}</p><div class="metric-sub">${{sub}}</div>`;
          els.metricGrid.appendChild(card);
        }}
      }}

      function renderTable(rows) {{
        els.body.innerHTML = "";
        if (!rows.length) {{
          const tr = document.createElement("tr");
          tr.innerHTML = `<td colspan="10" class="muted">No runs match the current filters.</td>`;
          els.body.appendChild(tr);
          return;
        }}
        for (const row of rows) {{
          const tr = document.createElement("tr");
          const accuracy = Number(row.summary.accuracy);
          tr.innerHTML = `
            <td>
              <div class="model-cell">
                <strong>${{row.model_display}}</strong>
                <span class="muted">${{row.reasoning_effort || "none"}} · ${{row.created_at.slice(0, 16).replace("T", " ")}}</span>
              </div>
            </td>
            <td>
              <div class="accuracy-stack">
                <span class="pill ${{accuracyClass(accuracy)}}">${{accuracy.toFixed(1)}}%</span>
                <div class="bar"><span style="width:${{Math.max(0, Math.min(100, accuracy))}}%; background:${{accuracy >= 80 ? "var(--good)" : accuracy >= 50 ? "var(--warn)" : "var(--bad)"}}"></span></div>
              </div>
            </td>
            <td>${{row.summary.passed}}</td>
            <td>${{row.summary.failed}}</td>
            <td>${{fmtSeconds(row.summary.avg_latency_s)}}</td>
            <td>${{fmtSeconds(row.summary.p95_latency_s)}}</td>
            <td>${{fmtCost(row.summary.total_cost)}}</td>
            <td>${{fmtCost(row.summary.avg_cost)}}</td>
            <td><span class="pill">${{row.revision_label}}</span></td>
            <td class="failed-cases">${{row.failed_cases.length ? row.failed_cases.join(", ") : "—"}}</td>
          `;
          els.body.appendChild(tr);
        }}
      }}

      function renderScopeMeta(section) {{
        els.scopeTitle.textContent = `${{section.case_count}} cases · feature=${{section.feature_filter || "all"}}`;
        els.scopeMeta.textContent = `Case set ${{section.case_set_id.slice(0, 12)}} · latest ${{section.latest_created_at.replace("T", " ").slice(0, 16)}}`;
        els.scopeCases.innerHTML = "";
        for (const caseId of section.case_ids) {{
          const li = document.createElement("li");
          li.textContent = caseId;
          els.scopeCases.appendChild(li);
        }}
      }}

      function render() {{
        const section = getSection();
        const rows = applyFilters(section);
        renderScopeMeta(section);
        renderSummary(rows);
        renderTable(rows);
      }}

      const scopeOptions = payload.sections.map((section) => ({{
        value: section.case_set_id,
        label: scopeOptionLabel(section)
      }}));
      buildOptions(els.scope, scopeOptions, state.scopeId);

      function refreshDynamicOptions() {{
        const section = getSection();
        const models = Array.from(new Set(section.runs.map((row) => row.model))).sort();
        const reasoning = Array.from(new Set(section.runs.map((row) => row.reasoning_effort || "none"))).sort();
        buildOptions(els.model, [{{ value: "all", label: "All models" }}, ...models.map((model) => ({{ value: model, label: model.split("/").pop() }}))], state.model);
        buildOptions(els.reasoning, [{{ value: "all", label: "All reasoning levels" }}, ...reasoning.map((value) => ({{ value, label: value }}))], state.reasoning);
      }}

      els.scope.addEventListener("change", (event) => {{
        state.scopeId = event.target.value;
        state.model = "all";
        state.reasoning = "all";
        refreshDynamicOptions();
        render();
      }});
      els.model.addEventListener("change", (event) => {{ state.model = event.target.value; render(); }});
      els.reasoning.addEventListener("change", (event) => {{ state.reasoning = event.target.value; render(); }});
      els.sort.addEventListener("change", (event) => {{ state.sort = event.target.value; render(); }});
      els.latestOnly.addEventListener("change", (event) => {{ state.latestOnly = event.target.checked; render(); }});
      els.failedOnly.addEventListener("change", (event) => {{ state.failedOnly = event.target.checked; render(); }});
      els.dirtyOnly.addEventListener("change", (event) => {{ state.dirtyOnly = event.target.checked; render(); }});
      els.query.addEventListener("input", (event) => {{ state.query = event.target.value; render(); }});
      refreshDynamicOptions();
      render();
    }}
  </script>
</body>
</html>"""


def write_leaderboard_html(
    runs: list[dict[str, Any]],
    html_path: Path | None = None,
) -> str:
    """Write the rendered leaderboard HTML to disk."""
    path = html_path or HTML_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_leaderboard_html(runs)
    path.write_text(content, encoding="utf-8")
    return content


def record_run(
    *,
    results: list[EvalResult],
    case_ids: list[str],
    model: str,
    reasoning_effort: str | None,
    max_tool_iterations: int,
    feature_filter: str | None,
    allow_duplicate: bool = False,
    runs_path: Path | None = None,
    markdown_path: Path | None = None,
    html_path: Path | None = None,
    repo_context: dict[str, Any] | None = None,
) -> RecordRunOutcome:
    """Persist one eval run and regenerate the Markdown leaderboard."""
    path = runs_path or RUNS_PATH
    markdown = markdown_path or MARKDOWN_PATH
    html_output = html_path or HTML_PATH
    runs = load_run_records(path)
    record = build_run_record(
        results=results,
        case_ids=case_ids,
        model=model,
        reasoning_effort=reasoning_effort,
        max_tool_iterations=max_tool_iterations,
        feature_filter=feature_filter,
        repo_context=repo_context,
    )
    duplicate = next(
        (
            existing
            for existing in runs
            if existing.get("run_fingerprint") == record["run_fingerprint"]
        ),
        None,
    )
    if duplicate is not None and not allow_duplicate:
        write_leaderboard_markdown(runs, markdown)
        write_leaderboard_html(runs, html_output)
        return RecordRunOutcome(
            recorded=False,
            record=duplicate,
            duplicate_of=str(duplicate.get("run_id")),
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    runs.append(record)
    write_leaderboard_markdown(runs, markdown)
    write_leaderboard_html(runs, html_output)
    return RecordRunOutcome(recorded=True, record=record)


def main() -> None:
    """CLI entry point for leaderboard rendering."""
    parser = argparse.ArgumentParser(description="Render the eval leaderboard.")
    subparsers = parser.add_subparsers(dest="command")
    render_parser = subparsers.add_parser(
        "render",
        help="Render leaderboard.md from recorded JSONL run history.",
    )
    render_parser.add_argument(
        "--runs-path",
        type=Path,
        default=RUNS_PATH,
        help="Path to the JSONL leaderboard history.",
    )
    render_parser.add_argument(
        "--markdown-path",
        type=Path,
        default=MARKDOWN_PATH,
        help="Path to the generated leaderboard markdown.",
    )
    render_html_parser = subparsers.add_parser(
        "render-html",
        help="Render leaderboard.html from recorded JSONL run history.",
    )
    render_html_parser.add_argument(
        "--runs-path",
        type=Path,
        default=RUNS_PATH,
        help="Path to the JSONL leaderboard history.",
    )
    render_html_parser.add_argument(
        "--html-path",
        type=Path,
        default=HTML_PATH,
        help="Path to the generated leaderboard HTML.",
    )
    args = parser.parse_args()
    if args.command == "render":
        runs = load_run_records(args.runs_path)
        write_leaderboard_markdown(runs, args.markdown_path)
        print(
            f"Rendered leaderboard with {len(runs)} run(s) to {args.markdown_path}",
        )
        return
    if args.command == "render-html":
        runs = load_run_records(args.runs_path)
        write_leaderboard_html(runs, args.html_path)
        print(
            f"Rendered HTML leaderboard with {len(runs)} run(s) to {args.html_path}",
        )
        return
    if args.command is None:
        parser.print_help()
        raise SystemExit(2)
    parser.print_help()
    raise SystemExit(2)


def _build_summary_metrics(results: list[EvalResult]) -> dict[str, Any]:
    """Build summary metrics from a batch of eval results."""
    executions = [
        result.execution for result in results if result.execution is not None
    ]
    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    accuracy = (passed / len(results) * 100.0) if results else 0.0
    latencies = [execution.latency_s for execution in executions]
    costs = [execution.cost for execution in executions if execution.cost is not None]
    return {
        "accuracy": accuracy,
        "passed": passed,
        "failed": failed,
        "avg_latency_s": sum(latencies) / len(latencies) if latencies else None,
        "p95_latency_s": _percentile_nearest_rank(latencies, 0.95)
        if latencies
        else None,
        "total_cost": sum(costs) if costs else None,
        "avg_cost": (sum(costs) / len(costs)) if costs else None,
        "input_tokens": sum(execution.input_tokens for execution in executions),
        "output_tokens": sum(execution.output_tokens for execution in executions),
        "total_tokens": sum(execution.total_tokens for execution in executions),
        "cache_hits": sum(execution.cache_hits for execution in executions),
        "cache_misses": sum(execution.cache_misses for execution in executions),
    }


def _build_case_result(result: EvalResult) -> dict[str, Any]:
    """Build one per-case leaderboard outcome row."""
    execution = result.execution
    return {
        "case_id": result.case_id,
        "passed": result.passed,
        "failure_names": [failure.name for failure in result.failures],
        "error": result.error,
        "latency_s": execution.latency_s if execution is not None else None,
        "cost": execution.cost if execution is not None else None,
    }


def _group_runs_for_sections(runs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group recorded runs into comparable leaderboard sections."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        grouped.setdefault(str(run["case_set_id"]), []).append(run)
    sections = list(grouped.values())
    sections.sort(
        key=lambda section: (
            -int(section[0].get("case_count", 0)),
            -max(_created_at_timestamp(run) for run in section),
        ),
    )
    return sections


def _build_sections(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build sorted leaderboard sections with ranked latest rows."""
    sections: list[dict[str, Any]] = []
    for section_runs in _group_runs_for_sections(runs):
        first = section_runs[0]
        sections.append(
            {
                "case_set_id": str(first["case_set_id"]),
                "case_count": int(first["case_count"]),
                "feature_filter": first.get("feature_filter"),
                "case_ids": list(first.get("case_ids", [])),
                "latest_created_at": max(
                    str(run.get("created_at", "")) for run in section_runs
                ),
                "rows": _rank_section_rows(section_runs),
                "runs": sorted(
                    section_runs,
                    key=lambda run: str(run.get("created_at", "")),
                    reverse=True,
                ),
            }
        )
    return sections


def _rank_section_rows(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one latest row per model/reasoning pair, ranked for display."""
    latest_by_identity: dict[tuple[str, str | None], dict[str, Any]] = {}
    for run in sorted(
        runs, key=lambda run: str(run.get("created_at", "")), reverse=True
    ):
        key = (str(run["model"]), run.get("reasoning_effort"))
        latest_by_identity.setdefault(key, run)
    rows = list(latest_by_identity.values())
    rows.sort(
        key=lambda run: (
            -float(run["summary"]["accuracy"]),
            int(run["summary"]["failed"]),
            _sort_optional_number(run["summary"].get("avg_cost")),
            _sort_optional_number(run["summary"].get("avg_latency_s")),
        )
    )
    return rows


def _display_reasoning_effort(value: str | None) -> str:
    """Render a stored reasoning effort for leaderboard display."""
    return value or "none"


def _format_percent(value: float) -> str:
    """Format a percentage value for markdown output."""
    return f"{value:.1f}%"


def _format_optional_seconds(value: Any) -> str:
    """Format a seconds value or return a placeholder."""
    if value is None:
        return "-"
    return f"{float(value):.2f}s"


def _format_optional_cost(value: Any) -> str:
    """Format a USD cost value or return a placeholder."""
    if value is None:
        return "-"
    return f"${float(value):.4f}"


def _format_revision(run: dict[str, Any]) -> str:
    """Format a compact git revision label for leaderboard display."""
    sha = str(run.get("git_sha", "unknown"))
    dirty = bool(run.get("dirty", False))
    short_sha = sha[:7] if sha != "unknown" else sha
    return f"{short_sha}*" if dirty else short_sha


def _format_failed_cases(run: dict[str, Any]) -> str:
    """Format failed case ids for one leaderboard row."""
    failed_case_ids = [
        str(case["case_id"])
        for case in run.get("per_case", [])
        if not bool(case.get("passed", False))
    ]
    return ", ".join(failed_case_ids) if failed_case_ids else "-"


def _build_html_payload(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the normalized client payload for the HTML leaderboard."""
    sections = []
    for section in _build_sections(runs):
        sections.append(
            {
                "case_set_id": section["case_set_id"],
                "case_count": section["case_count"],
                "feature_filter": section["feature_filter"],
                "case_ids": section["case_ids"],
                "latest_created_at": section["latest_created_at"],
                "runs": [_run_for_html(run) for run in section["runs"]],
            }
        )
    return {"sections": sections}


def _run_for_html(run: dict[str, Any]) -> dict[str, Any]:
    """Normalize one recorded run for the HTML client."""
    return {
        "run_id": run["run_id"],
        "created_at": run["created_at"],
        "model": run["model"],
        "model_display": str(run["model"]).split("/")[-1],
        "reasoning_effort": run.get("reasoning_effort"),
        "summary": run["summary"],
        "git_sha_short": str(run.get("git_sha", "unknown"))[:7],
        "dirty": bool(run.get("dirty", False)),
        "revision_label": _format_revision(run),
        "failed_cases": [
            str(case["case_id"])
            for case in run.get("per_case", [])
            if not bool(case.get("passed", False))
        ],
    }


def _git_output(command: list[str], cwd: Path) -> str | None:
    """Return stripped stdout for a git command, or None on failure."""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return completed.stdout.strip() or None


def _percentile_nearest_rank(values: list[float], percentile: float) -> float:
    """Return the nearest-rank percentile for a non-empty numeric list."""
    sorted_values = sorted(values)
    rank = max(1, int((percentile * len(sorted_values)) + 0.999999999))
    return sorted_values[rank - 1]


def _sort_optional_number(value: Any) -> float:
    """Return a sortable numeric value, pushing missing values last."""
    if value is None:
        return float("inf")
    return float(value)


def _stable_hash(payload: Any) -> str:
    """Return a stable sha256 hash for a JSON-serializable payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_now_iso() -> str:
    """Return an ISO timestamp in UTC suitable for persisted run records."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _created_at_timestamp(run: dict[str, Any]) -> float:
    """Return a sortable timestamp for a persisted run record."""
    raw = str(run.get("created_at", "")).replace("Z", "+00:00")
    return datetime.fromisoformat(raw).timestamp()


if __name__ == "__main__":
    main()
