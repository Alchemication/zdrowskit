# zdrowskit public site — source

The landing page for <https://alchemication.github.io/zdrowskit/>. The docs and
eval sections of that site are generated from `docs/` and
`evals/leaderboard/runs.jsonl`, not stored here.

## Build the whole site

```bash
uv run --group site python marketing/build.py
python3 -m http.server -d _site 8765
```

`_site/` is gitignored and rebuilt from scratch on every run. CI does the same
via `.github/workflows/pages.yml`.

## What lives here

- `assets/base.css` — **the single source of truth for the palette and site
  chrome.** Never linked with `<link>`; every page inlines it at build time so
  each output file stays self-contained. Three consumers: the landing page
  (`{{BASE_CSS}}`), the docs template in `marketing/build.py`, and
  `evals/leaderboard/html.py`, which reads the file directly. Do not redeclare
  a colour token anywhere else — that is how the leaderboard drifted into
  looking like a different product the first time.
- `index.html` — the landing page. Page-specific CSS only; the shared chrome
  arrives via the token. Opening it straight from disk looks unstyled, which is
  expected — build the site to view it.
- `assets/people-triptych.webp` — generated hero imagery. The generation brief
  is in `../image-prompt.txt`.
- `assets/favicon.svg`.

## Build-time placeholders

`index.html` may contain `{{PLACEHOLDER}}` tokens, resolved by
`landing_placeholders()` in `marketing/build.py`. The build fails rather than
publishing an unresolved token. Currently:

| Token | Source |
| --- | --- |
| `{{BASE_CSS}}` | `assets/base.css`, inlined |
| `{{EVAL_CASE_COUNT}}` | Distinct case ids in the latest recorded eval run |
| `{{EVAL_UPDATED}}` | Date of that run |

Never write a placeholder token literally inside `base.css`: it is inlined into
the landing page, so the token would reappear after substitution and fail the
unresolved-token check.

## Conventions worth keeping

- The example Telegram messages are illustrative, and the page says so. If they
  are ever replaced with real output, redact it and keep the disclaimer honest.
- The privacy section deliberately matches the bluntness of `README.md`: raw
  data is local, but slices do reach the configured LLM provider. Do not soften
  it.
