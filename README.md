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

## Refresh schedule

Windows Task Scheduler jobs under `\WhatTheFed\` keep the local database current:

| Task | Cadence | Script |
| --- | --- | --- |
| Market Ingestion | Daily 08:00 | `scripts/run-market-ingestion.ps1` |
| Macro Ingestion (CPI + PPI + labor + GDP + fiscal) | Daily 06:30 | `scripts/run-macro-ingestion.ps1` |
| Treasury Ingestion (curve + breakevens) | Weekdays 15:00 | `scripts/run-treasury-ingestion.ps1` |
| Policy Rates Ingestion | Weekdays 07:00 | `scripts/run-policy-rates-ingestion.ps1` |

Register them with the matching `scripts/register-*-task.ps1` scripts.

The public dashboard is refreshed by `.github/workflows/deploy-pages.yml` at 15:30 UTC
daily and 23:30 UTC on weekdays. It also runs after relevant pushes to `main` or
manually through GitHub Actions. Configure the repository's Pages source as
**GitHub Actions** before the first deployment.

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
