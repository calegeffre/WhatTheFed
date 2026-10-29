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
If any required payload is missing or invalid, the headline and affected data
tiles display `?` with an error instead of substituting fallback values.

Data at a Glance is a nested treemap grouped into Market Data (Kalshi and
Polymarket), Inflation, Labor, Growth & Fiscal, Rates & Funding, and Policy
Context. Signal summaries use a continuous 1-to-5 policy-pressure scale:
1 favors lower rates, 3 is balanced, and 5 favors higher rates. Signed model
biases are mapped linearly to this display scale and rounded to one decimal;
forecast calculations are unchanged. Market scores use raise probability minus
cut probability, with actual outcome odds shown separately. Original economic
readings retain their units. Select a tile to inspect its date, readings, and
source. Rectangle areas are presentation choices, not model weights or odds.
Policy Context shows the last statement's existing 1-to-5 scores, not new data.

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
backtest instead. Recent official statement target ranges are merged into that
archive without removing older meetings. The backtest is refreshed when a newly
completed decision or a corrected historical outcome is available; failures
retain the last successful results. The rate decision chart also reads official
statement targets independently, so recent rates remain visible when FRED or
unrelated imports fail. Unparseable ranges are omitted, not estimated. Without
an older archive, coverage begins with the available parsed statements.
To rebuild the full archive,
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

Dual Mandate is the first/default Signal Knowledge Graph experiment. Its factor
circles are sized by fixed macro-model weight, keeping labor prominent even when
its directional bias is near neutral. Factor colors and visible labels show
less, neutral, or more hike pressure. Mandate-hub percentages summarize weighted
signal attention, not decision probabilities; this visualization does not change
forecast weights or estimate the impact of employment-data revisions.

- **Python** for ingestion/export pipelines
- **SQLite** for local persistence
- **HTML/CSS/JavaScript** for the static dashboard
- **Three.js** for the 3D knowledge graph

## Meeting evidence in Previous Meeting History (Neo4j Aura)

Each meeting in Previous Meeting History has an expandable **Meeting evidence**
card. Open it to load that meeting's economic inputs, observation periods,
estimated availability dates, model biases, and official source links. Each
signal also has an expandable view of its underlying values. There is no separate
explorer tab or graph navigation.

Evidence covers meetings in the five-year backtest; other meetings explicitly
report that evidence is unavailable rather than substituting current data.
The cards use the same static Neo4j graph export and
lag-based observation selection as that backtest. Estimated availability dates
are **not verified original release timestamps**, and values can include later
revisions. Forecasts are retrospective reconstructions, not archived live
forecasts. Edges show model provenance, not economic causation. Historical
market prices, speeches, and individual votes are not included yet.

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

Use the actual database name from Aura; it is not necessarily `neo4j`. Environment
variables override values in the file. Never put credentials in dashboard
JavaScript or commit the credentials file.

```powershell
python scripts\build-static-site.py --output-dir _site --neo4j-env-file .env.neo4j
```

The build writes project-scoped `WTFMeetingEvidence` nodes and typed relationships
to Aura, then queries and verifies the complete graph before exporting
`data/meeting_evidence_data.js`. A content hash identifies each immutable
snapshot; identical imports are idempotent. Different source-data snapshots are
retained, so storage grows with changed imports. Nothing deletes or changes
other database labels. The Neo4j account needs read/write and constraint-creation
permissions for the initial setup.

Nodes also carry typed labels such as `WTFMeeting`, `WTFReconstruction`,
`WTFSignal`, `WTFObservation`, and `WTFSource`. To inspect a meeting in Aura's
graph interface, substitute a date from the dashboard and run:

```cypher
MATCH path = (m:WTFMeeting {meeting_date: '2026-09-16'})-[*1..4]->(e)
RETURN path
LIMIT 100
```

If multiple snapshots exist, filter `m.snapshot_id` using the `snapshot_id`
in the exported evidence payload to inspect one exact version.

For hosted builds, configure repository Actions secrets named `NEO4J_URI`,
`NEO4J_USERNAME`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE`. The workflow installs the
optional driver when `NEO4J_URI` is set. Configured connection/import failures
stop the build; unconfigured builds explicitly show meeting evidence as unavailable.
If the backtest has no completed meetings, the build instead publishes an
explicitly unavailable evidence view and a partial-refresh warning, without attempting
an Aura import. Enable both `refresh_fed_history` and `refresh_backtest` in a
manual workflow run to rebuild the full archives. Routine runs can populate
recent evidence once official statement ranges and complete lagged macro
histories are available; adding Aura secrets alone does not supply missing data.
Only public economic evidence and source metadata belong in this graph: its
export is published on GitHub Pages. The browser never connects to Aura or
receives credentials.

To import/query an already-generated set of payloads:

```powershell
python -m whatthefed.meeting_evidence --data-dir _site\data --env-file .env.neo4j --output-js _site\data\meeting_evidence_data.js
```
