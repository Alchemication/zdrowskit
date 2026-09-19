# zdrowskit public site — source

The landing page for <https://alchemication.github.io/zdrowskit/>. The docs and
eval sections of that site are generated from `docs/` and
`evals/leaderboard/runs.jsonl`, not stored here.

## Build the whole site

```bash
uv run --group site python marketing/build.py
python3 -m http.server -d _site 8765
```

## Diagrams

Mermaid blocks in `docs/*.md` are pre-rendered to SVG and inlined, so the
published pages carry no Mermaid runtime — the browser bundle is ~3.5 MB, which
is far more than eight flowcharts are worth. The Markdown keeps the Mermaid
source, so GitHub still renders it natively in the repo.

After changing any ```mermaid block:

```bash
uv run python marketing/render_diagrams.py    # needs Chrome; CHROME_BIN overrides
uv run python marketing/render_diagrams.py --check   # CI-style assertion only
```

SVGs are content-addressed by the hash of their source and committed under
`assets/diagrams/`. `marketing/build.py` fails if a block has no matching SVG,
so an edited diagram cannot silently ship stale.

`_site/` is gitignored and rebuilt from scratch on every run. CI does the same
via `.github/workflows/pages.yml`.

## What lives here

- `assets/base.css` — **the single source of truth for the palette, the type
  and the site chrome.** Never linked with `<link>`; every page inlines it at
  build time so each output file stays self-contained. Three consumers: the
  landing page (`{{BASE_CSS}}`), the docs template in `marketing/build.py`, and
  `evals/leaderboard/html.py`, which reads the file directly. Do not redeclare
  a colour token anywhere else — that is how the leaderboard drifted into
  looking like a different product the first time. It also defines the two
  shared surfaces: `.plate`, the chamfered stroke-and-fill card, and
  `.halftone`, the dot screen for dark panels.
- `assets/fonts/` — IBM Plex Sans (400, 500) and Plex Mono (400, 500), subset
  to Latin, about 70 KB in total. `base.css` names them from the site root;
  pages below the root rewrite the `assets/` prefix for their depth when they
  inline the stylesheet. Same-origin, so pages still make no external
  requests.
- `index.html` — the landing page. Page-specific CSS only; the shared chrome
  arrives via the token. Opening it straight from disk looks unstyled, which is
  expected — build the site to view it. The hero's loop is a small inline
  script; without JavaScript or with reduced motion it shows the ring with the
  Telegram message, still.
- `assets/og.png` — the link-preview image, a 1200×630 capture of the hero
  at rest. Recapture it after a visible hero change.
- `assets/bot-avatar.webp` — the Telegram bot's avatar, which the site's
  palette and shapes are drawn from. Source and prompts in `../bot-avatar/`.
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
