# WhatTheFed

WhatTheFed is a lightweight Fed dashboard that combines official policy text, macro data, and market-implied odds.

## Data sources

- **Federal Reserve**: official FOMC statement pages
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
preserves a neutral preference for holding. Experiment 7 applies empirical
shocks to this baseline over 520 deterministic Monte Carlo draws.

## Refresh schedule

GitHub Actions is the primary ingestion and deployment path. The
`.github/workflows/deploy-pages.yml` workflow rebuilds every data source using
disposable SQLite, then deploys the generated browser assets to GitHub Pages.

| Trigger | Cadence |
| --- | --- |
| Scheduled morning refresh | Daily at 15:30 UTC |
| Scheduled afternoon refresh | Weekdays at 23:30 UTC |
| Relevant changes on `main` | After each push |
| Manual refresh | `workflow_dispatch` from GitHub Actions |

The PowerShell ingestion scripts remain available for manually refreshing the
local database. Windows Task Scheduler registration scripts are optional and
are not required for the hosted dashboard.

## Storage and outputs

- Data is stored in **SQLite** at `data/market_snapshots.db` (statements, macro observations, market snapshots, and derived records).
- Browser-ready payloads are exported to `data/*.js` and loaded by `index.html`.
- A graph payload is also exported for the knowledge-graph view.
- Hosted builds use a disposable SQLite database and upload only `index.html` plus generated JavaScript payloads; the database is never included in the Pages artifact.

Build the same static artifact locally with:

```powershell
python scripts\build-static-site.py --output-dir _site
```

## Tech stack

- **Python** for ingestion/export pipelines
- **SQLite** for local persistence
- **HTML/CSS/JavaScript** for the static dashboard
- **Three.js** for the 3D knowledge graph
