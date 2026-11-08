# WhatTheFed

WhatTheFed is a lightweight Fed dashboard that combines official policy text, macro data, and market-implied odds.

## Data sources

- **Federal Reserve**: official FOMC statement pages and monthly event calendars
- **BLS**: CPI, PPI, and labor time-series data
- **U.S. Treasury**: daily nominal and TIPS real yields; derived breakeven inflation
- **Treasury Fiscal Data**: Monthly Treasury Statement receipts, outlays, and deficit/surplus
- **BEA**: quarterly real GDP growth and major demand components
- **NY Fed**: overnight reference rates (EFFR, SOFR, OBFR, BGCR, TGCR) and the published FOMC target band
- **Kalshi + Polymarket**: market odds for upcoming FOMC outcomes

## Prediction model

The headline prediction, Monte Carlo, and sensitivity experiments share this ensemble:

```text
macro bias  = 24% CPI + 12% PPI + 21% labor + 12% GDP
              + 14% breakevens + 8% Treasury curve
              + 5% policy rates + 4% fiscal impulse
policy bias = carry-over × prior FOMC bias + (1 - carry-over) × macro bias
final bias  = 45% market bias + 55% policy bias
market bias = P(raise) - P(cut)
```

Inputs range from `-1` (dovish/cut) to `+1` (hawkish/raise). A softmax converts
the final bias to raise/hold/cut probabilities with a `0.30` hold preference.
Experiment 7 applies empirical shocks over 520 deterministic Monte Carlo draws.
Successful refreshes append probability snapshots, charted by target meeting.
Missing or invalid required inputs display `?` with an error, not fallback values.

Data at a Glance is a nested treemap grouped into Market Data (Kalshi and
Polymarket), Inflation, Labor, Growth & Fiscal, Rates & Funding, and Policy
Context. Scores map model biases linearly to a 1-to-5 pressure scale: 1 favors
lower rates, 3 is balanced, and 5 favors higher rates. Market scores use
`P(raise) - P(cut)`; odds and economic readings retain their units. Select a tile
for dates, readings, and sources. Tile areas are not model weights or odds.
Policy Context reflects the last FOMC statement, not current economic data.

## Refresh schedule

`.github/workflows/deploy-pages.yml` refreshes data using saved SQLite observations
and deploys the static dashboard to GitHub Pages.

| Trigger | Cadence |
| --- | --- |
| Scheduled morning refresh | Daily at 15:30 UTC |
| Scheduled afternoon refresh | Weekdays at 23:30 UTC |
| Relevant changes on `main` | After each push |
| Manual refresh | `workflow_dispatch` from GitHub Actions |

PowerShell scripts support local ingestion; Windows Task Scheduler is optional.

Source failures are retried up to three times without blocking other sources.
Failed sources retain saved payloads or remain unavailable, with a visible
partial-refresh warning. Probability snapshots require all current model inputs
to refresh successfully. If an existing archive cannot be retrieved and no saved
copy exists, publication stops to avoid erasing history; successful ingestion is
still saved for the next run.

Routine runs reuse the rate archive rather than download timeout-prone FRED
history. Official statement targets extend the archive and independently feed
the rate chart; unparseable ranges are omitted. Without a saved archive, coverage
begins with parsed statements. New or corrected outcomes trigger a backtest refresh.

To rebuild full history, enable `refresh_fed_history` and `refresh_backtest` in a
manual run (locally: `--refresh-fed-history --refresh-backtest`). Historical
refresh failures retain saved results and do not block successful current data.

## Storage and outputs

- Data is stored in **SQLite** at `data/market_snapshots.db` (statements, macro observations, market snapshots, and derived records).
- Browser-ready payloads are exported to `data/*.js` and loaded by `index.html`.
- Graph payloads support the knowledge-graph and meeting-evidence views.
- The Fed calendar supplies Board speeches, Chair highlights, and official livestream links.
- Hosted builds save observations and payloads in `.refresh-cache`, even after later failures. Actions cache can be evicted and is not a permanent backup.
- Pages publishes static HTML, assets, and generated JavaScript, never the database.
- Model probability history is preserved from cache or deployed Pages payloads.

Build the same static artifact locally with:

```powershell
python scripts\build-static-site.py --output-dir _site --cache-dir .refresh-cache
```

## Tech stack

- **Python** for ingestion/export pipelines
- **SQLite** for local persistence
- **HTML/CSS/JavaScript** for the static dashboard
- **Three.js** for the 3D knowledge graph

The default Dual Mandate graph sizes factors by model weight and colors them by
policy pressure. Hub percentages show weighted signal attention, not decision
probabilities; the visualization does not alter forecasts or estimate revision impacts.

## Meeting evidence in Previous Meeting History (Neo4j Aura)

Expandable **Meeting evidence** cards show economic inputs, observation periods,
estimated availability dates, model biases, and sources for the five-year backtest.
Other meetings explicitly report unavailable evidence.

Evidence uses the backtest's lag-based selection. Availability dates are **estimates,
not verified release timestamps**, and values may include revisions. Forecasts
are retrospective reconstructions, not archived live predictions. Graph edges
show provenance, not causation; historical market prices, speeches, and individual
votes are not included.

Install the optional driver:

```powershell
python -m pip install -r requirements-neo4j.txt
```

Create `.env.neo4j` locally (Git ignores it):

```text
NEO4J_URI=neo4j+s://YOUR_INSTANCE.databases.neo4j.io
NEO4J_USERNAME=YOUR_USERNAME
NEO4J_PASSWORD=YOUR_PASSWORD
NEO4J_DATABASE=YOUR_DATABASE_NAME
```

Use Aura's actual database name, not necessarily `neo4j`. Environment variables
override the file. Never commit credentials or put them in browser JavaScript.

```powershell
python scripts\build-static-site.py --output-dir _site --neo4j-env-file .env.neo4j
```

The build imports project-scoped `WTFMeetingEvidence` nodes and relationships,
verifies the graph, and exports `data/meeting_evidence_data.js`. Content-hashed
snapshots are immutable; identical imports are idempotent, while changed snapshots
increase storage. Other database labels are untouched. Initial setup requires
read/write and constraint-creation permissions.

To inspect a meeting in Aura, substitute its date:

```cypher
MATCH path = (m:WTFMeeting {meeting_date: '2026-09-16'})-[*1..4]->(e)
RETURN path
LIMIT 100
```

Filter `m.snapshot_id` by the exported payload's `snapshot_id` for an exact version.

For hosted builds, set Actions secrets with the four names above. Setting
`NEO4J_URI` enables driver installation; connection/import failures stop the build.
Without configuration or completed backtest meetings, evidence is explicitly
unavailable (empty backtests also produce a partial-refresh warning).

Aura credentials alone do not supply missing history. Enable `refresh_fed_history`
and `refresh_backtest` to rebuild archives; evidence requires statement targets
and complete lagged macro histories. **Only public economic data belongs in the
graph:** its export is published to Pages. Browsers never connect to Aura or
receive credentials.

To import/query an already-generated set of payloads:

```powershell
python -m whatthefed.meeting_evidence --data-dir _site\data --env-file .env.neo4j --output-js _site\data\meeting_evidence_data.js
```
