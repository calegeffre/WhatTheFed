# WhatTheFed

WhatTheFed is a lightweight Fed dashboard that combines official policy text, macro data, and market-implied odds.

## Data sources

- **Federal Reserve**: official FOMC statement pages and monthly event calendars
- **BLS**: CPI, PPI, and labor time-series data
- **U.S. Treasury**: daily par yield curve rates (1 Mo through 30 Yr), plus TIPS real yields used to derive breakeven inflation expectations
- **Treasury Fiscal Data**: Monthly Treasury Statement receipts, outlays, and deficit/surplus
- **BEA**: quarterly real GDP growth and major demand components
- **NY Fed**: overnight reference rates (EFFR, SOFR, OBFR, BGCR, TGCR) and the published FOMC target band
- **Kalshi + Polymarket**: market odds for upcoming FOMC outcomes

## Prediction model

The headline prediction uses the same all-data ensemble as the Monte Carlo and
sensitivity experiments:

```text
macro bias  = 24% CPI + 12% PPI + 21% labor + 12% GDP
              + 14% breakevens + 8% Treasury curve
              + 5% policy rates + 4% fiscal impulse
policy bias = carry-over × prior FOMC bias + (1 - carry-over) × macro bias
final bias  = 45% market bias + 55% policy bias
market bias = P(raise) - P(cut)
```

Every input is normalized to a `-1` (dovish/cut) to `+1` (hawkish/raise) scale.
The final bias is converted to raise/hold/cut probabilities with a softmax that
preserves a modest neutral preference for holding (`0.30` utility, reduced from
the original `0.45`). Experiment 7 applies empirical
shocks to this baseline over 520 deterministic Monte Carlo draws.
Each hosted refresh also appends the resulting raise/hold/cut probabilities to
the deployed model-history payload. The dashboard charts those snapshots by
target FOMC meeting so changes in the model can be followed into each decision.
If any required payload is missing or invalid, the headline and affected heat
cards display `?` with an error instead of substituting fallback values.

## Refresh schedule

GitHub Actions is the primary ingestion and deployment path. The
`.github/workflows/deploy-pages.yml` workflow rebuilds current data sources using
a saved SQLite database, then deploys the generated browser assets to GitHub Pages.

| Trigger | Cadence |
| --- | --- |
| Scheduled morning refresh | Daily at 15:30 UTC |
| Scheduled afternoon refresh | Weekdays at 23:30 UTC |
| Relevant changes on `main` | After each push |
| Manual refresh | `workflow_dispatch` from GitHub Actions |

The PowerShell ingestion scripts remain available for manually refreshing the
local database. Windows Task Scheduler registration scripts are optional and
are not required for the hosted dashboard.

Hosted ingestion retries a failed source up to three times for transient
provider errors. If all attempts fail, other sources continue refreshing. Successful
outputs and observations are saved in `.refresh-cache` and restored on the next
Actions run. Failed sources retain their last saved payload, or remain unavailable
if none exists; a visible partial-refresh notice warns that retained data may be
stale. Model probability snapshots are appended only when all required current
model inputs refresh successfully.
If a deployed archive cannot be retrieved and no saved copy exists, ingestion
still saves successful data, but publication stops to avoid replacing unknown
existing history with an empty archive. The next run retries retrieval.

Historical Fed target rates are not pulled from FRED on routine runs because
that download repeatedly times out. Builds reuse the deployed rate archive and
backtest instead. Recent FOMC decisions are saved and displayed independently of
that archive. If no rate archive exists, long-term rate charts and an uncached
backtest remain unavailable; current data still refreshes. To rebuild the archive,
select `refresh_fed_history` in a manual workflow run (or pass
`--refresh-fed-history` locally). Select `refresh_backtest` as well to recalculate
the backtest. Historical refreshes run after current ingestion; a timeout retains
the saved archive and does not prevent deployment of successful current data.

## Storage and outputs

- Data is stored in **SQLite** at `data/market_snapshots.db` (statements, macro observations, market snapshots, and derived records).
- Browser-ready payloads are exported to `data/*.js` and loaded by `index.html`.
- A graph payload is also exported for the knowledge-graph view.
- The official Fed calendar is checked for upcoming speeches from all Board speakers, with Chair events highlighted and official livestream links retained.
- Hosted builds restore and save `.refresh-cache` with Actions cache, including SQLite observations and browser payloads, even if a later build step fails. Actions caches can be evicted; this is not a permanent backup. Only `index.html` plus generated JavaScript payloads are uploaded to Pages; the database is never included.
- Model probability history is carried forward from saved data or the currently deployed Pages payload and extended only after successful current-source ingestion. Retrieval failures retain saved history rather than erase it.

Build the same static artifact locally with:

```powershell
python scripts\build-static-site.py --output-dir _site --cache-dir .refresh-cache
```

## Tech stack

- **Python** for ingestion/export pipelines
- **SQLite** for local persistence
- **HTML/CSS/JavaScript** for the static dashboard
- **Three.js** for the 3D knowledge graph
