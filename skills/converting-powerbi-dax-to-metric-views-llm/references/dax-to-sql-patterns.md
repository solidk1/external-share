# DAX → Metric View SQL Translation Patterns

This reference is the **agent's translation cookbook** — the patterns the LLM applies when filling in `AGENT_TRANSLATE_DAX` placeholders in the scaffolded YAML. Covers: how CALCULATE filters become FILTER clauses, how snapshot flags get added to source SQL, how VAR/RETURN inlining works, and the time-intel patterns that need extra care. For the at-a-glance table of which DAX constructs map to which SQL forms, see the **Translation cookbook** table in `SKILL.md`.

## CALCULATE → FILTER (WHERE …)

`CALCULATE` with one or more boolean filter arguments becomes a SQL aggregate with a `FILTER (WHERE …)` clause. `FILTER('T', cond)` arguments are unwrapped to just `cond`.

| DAX | Metric view SQL |
|-----|-----------------|
| `CALCULATE(SUM('S'[Amt]), 'S'[Status] = "O")` | `SUM(\`Amt\`) FILTER (WHERE \`Status\` = 'O')` |
| `CALCULATE(SUM('S'[Amt]), FILTER('S', 'S'[Date] >= "2024-01-01"))` | `SUM(\`Amt\`) FILTER (WHERE \`Date\` >= '2024-01-01')` |
| `CALCULATE(SUM('S'[Amt]), 'S'[A]=1, 'S'[B]=2)` | `SUM(\`Amt\`) FILTER (WHERE \`A\`=1 AND \`B\`=2)` |
| `CALCULATE(SUM('S'[Amt]), LASTDATE('Cal'[Date]))` | `SUM(\`Amt\`) FILTER (WHERE is_latest_snapshot)` (also augment `source:` SQL — see below) |

## LASTDATE / FIRSTDATE → snapshot flag in source SQL

When the original DAX has `LASTDATE(...)` or `FIRSTDATE(...)` as a CALCULATE filter, the agent does two things:

1. Replace the filter with `is_latest_snapshot` / `is_first_snapshot` in the measure expr.
2. Rewrite the metric view's `source:` from a plain table reference to an inline SELECT that adds the boolean flag column:

```yaml
source: |
  SELECT
    f.*,
    (`data_cutoff_dt` = MAX(`data_cutoff_dt`) OVER ()) AS is_latest_snapshot
  FROM main.sales.fact_revenue f
```

The fact's date column is detected by:
1. An active relationship from fact to a calendar-named dim (preferred).
2. Any fact column with `date` or `snapshot` in its name and dateTime type.
3. Any dateTime column on the fact.

If none match, `data_cutoff_dt` is used as a default and a warning is emitted.

## VAR / RETURN inlining

`VAR <name> = <expr>` bindings followed by `RETURN <body>` are inlined: every reference to `<name>` in `<body>` (and in subsequent VARs) is replaced by `(<expr>)`. Works for arbitrary nesting because substitution is recursive.

```dax
VAR Required = [Target] - [Actual]
VAR Output = IF(Required < 0, 0, Required)
RETURN Output
```

becomes:

```sql
CASE WHEN ((MEASURE(`Target`)) - (MEASURE(`Actual`))) < 0
     THEN 0
     ELSE ((MEASURE(`Target`)) - (MEASURE(`Actual`)))
END
```

If you cannot inline cleanly (e.g., `VAR x = SUMX(...) RETURN ...` where SUMX itself has no clean SQL form), preserve the original DAX as a comment in the YAML and flag the measure for a manual rewrite.

## Forward-reference resolution

Databricks metric views require backward-only `MEASURE(\`X\`)` references at view-creation time. When the agent fills in measures:

1. Order the measures so each `MEASURE(\`X\`)` ref points to a measure defined earlier in the YAML.
2. If a true cycle exists (rare), inline the simpler referenced expression directly into the cycle-breaker.

Getting the order wrong yields `[UNRESOLVED_COLUMN]` errors at CREATE VIEW time.

## Period-over-period patterns

When a Power BI model defines several time-shift measures together (Monthly, YTD, MoM, YoY-by-month, QoQ-vs-LY), they **cannot all live in a single metric-view query** — the constraint is structural, not an LLM-translation limitation. Document this up front so consumers don't expect a 1:1 visual match.

### Three approaches, all valid

You can implement PoP measures inside the metric view three ways. Pick one consistently — don't mix:

1. **Native `window:` directive on a measure.** Cleanest when you have a date dimension on the join. Add a single date dimension; every PoP measure references it.

   ```yaml
   joins:
     - name: cal
       source: main.sales.dim_d_calendar
       on: "source.`snapshot_date` = cal.`date`"
   dimensions:
     - name: snapshot_date
       expr: cal.`date`
   measures:
     - name: dtd_shipment_prev
       expr: SUM(`daily_shpmt_net_rev_amt`) / 1000000
       window:
         - order: snapshot_date
           range: trailing
           offset: -1
   ```

2. **SQL `source:` with `LAG()` columns.** Push PoP logic into the source query so measures see prior-period columns directly. No new dimensions required (the LAG is computed row-by-row; consumers don't slice by it).

   ```yaml
   source: |
     SELECT
       f.*,
       LAG(daily_shpmt_net_rev_amt) OVER (
         PARTITION BY bg_cd, geo_cd, product_bu  -- all dim keys
         ORDER BY data_cutoff_dt
       ) AS prev_daily_shpmt_net_rev_amt
     FROM main.sales.fact_revenue f
   measures:
     - name: dtd_shipment
       expr: SUM(`prev_daily_shpmt_net_rev_amt`) / 1000000
   ```

3. **JOIN-CTE consumer view on top of the metric view.** When you need multiple windows in one query, expose a `*_kpi_wide` view that joins three CTEs — each querying the metric view at its own grain.

### Use `MEASURE()` refs in windowed measures for single-source-of-truth

Windowed measures (`window:` block) accept `MEASURE()` refs in `expr:` — they are NOT limited to raw aggregates. The window engine resolves the referenced measure's aggregate first, then applies the window. This means a YTD/SPLY/MoM time-intel measure can reuse its non-windowed sibling instead of re-inlining the formula:

```yaml
# Non-windowed base measure
- name: Gross Margin
  expr: "MEASURE(`Total Revenue`) - MEASURE(`Total COGS`)"

# Windowed sibling — single line, references the base
- name: Gross Margin SPLY
  expr: "MEASURE(`Gross Margin`)"
  window:
    - order: Year Start
      range: trailing 1 year
      semiadditive: last

- name: YTD Gross Margin
  expr: "MEASURE(`Gross Margin`)"
  window:
    - order: Date
      range: cumulative
      semiadditive: last
    - order: Year
      range: current
      semiadditive: last
```

This is the right shape: change `Total COGS` once and every YTD/SPLY/YoY measure that depends on `Gross Margin` (or transitively on `Total COGS`) tracks automatically. Don't re-inline raw `SUM()` formulas inside windowed measures — that breaks single-source-of-truth.

### Why one GROUP BY can't carry every shift in approach 1

Each window measure partitions by *every GROUP BY dim that isn't its own `order` dim*. That means:

| Measure | Required GROUP BY shape |
|---------|-------------------------|
| MoM lag (`trailing 1 month` on `Month Date`) | `Month Date` IN GROUP BY |
| YoY by month (`trailing 1 year` on `Year Start`) | `Month Of Year` IN GROUP BY, `Month Date` ABSENT (else the year-trailing partition forces year equality and goes NULL) |
| QoQ vs LY (`trailing 1 year` on `Year Start`) | `Quarter Of Year` IN GROUP BY |

Three measures, three incompatible GROUP BY shapes. SQL's single-aggregation-context model (which metric views inherit) cannot reconcile them in one query — DAX *can* because each measure re-shapes its own filter context lazily (`CALCULATE` + `SAMEPERIODLASTYEAR`).

### Recommended consumer shape (approach 3)

Define every PoP comparator as its own measure, then in consumer SQL JOIN three CTEs that each query the metric view at the grain its windows need:

```sql
WITH mom AS (
  SELECT `Month Date` AS month_date,
         EXTRACT(YEAR FROM `Month Date`) AS yr,
         EXTRACT(MONTH FROM `Month Date`) AS mo,
         EXTRACT(QUARTER FROM `Month Date`) AS qtr,
         MEASURE(`Monthly Sales`) AS monthly_sales,
         MEASURE(`YTD Sales`) AS ytd_sales,
         MEASURE(`MoM Growth %`) AS mom_pct
  FROM cat.sch.deals_metrics
  GROUP BY ALL
),
yoy AS (
  SELECT EXTRACT(YEAR FROM `Year Start`) AS yr,
         `Month Of Year` AS mo,
         MEASURE(`YoY Growth %`) AS yoy_pct
  FROM cat.sch.deals_metrics
  GROUP BY ALL
),
qoq AS (
  SELECT EXTRACT(YEAR FROM `Year Start`) AS yr,
         `Quarter Of Year` AS qtr,
         MEASURE(`QoQ Growth %`) AS qoq_pct
  FROM cat.sch.deals_metrics
  GROUP BY ALL
)
SELECT m.*, y.yoy_pct, q.qoq_pct
FROM mom m
LEFT JOIN yoy y USING (yr, mo)
LEFT JOIN qoq q USING (yr, qtr)
ORDER BY m.month_date;
```

Wrap it as a SQL view (`*_kpi_wide`) so analysts get a flat result set without rewriting the metric view.

### Why approach 3 beats plain `LAG(...) OVER (...)` against the source table

The window-function form is shorter, but you lose the metric view as a single source of truth — every consumer (Genie, dashboards, alerts, SQL editor) re-implements the same shifts and drifts independently. Keep the metric view; let the wide view be a thin convenience layer on top. (Approach 2 is fine when PoP is the *only* derivation needed and the LAG belongs to the metric view's contract.)

### Critical: `Year` must be a DATE for year-trailing windows

`order: Year` with `range: trailing 1 year` requires the engine to subtract `INTERVAL '-1' YEAR` from the dim value. An `EXTRACT(YEAR FROM ...)` INT will fail with `BINARY_OP_DIFF_TYPES`. Use `DATE_TRUNC('YEAR', date)` for the windowed dim — name it `Year Start` if you also want a separate display-friendly INT `Year`.

## Snowflake / multi-hop joins

The scaffolder only emits **direct** relationships from the fact table to dim tables. Snowflake schemas (dim → outer-dim) need:

1. The metric view spec at v1.1 with **DBR 17.1+** (nested joins).
2. Manually nest the second-hop join under the first in the YAML.

See SKILL.md § "Metric view YAML primer" → "Joins (snowflake, DBR 17.1+)".

## Bidirectional / many-to-many relationships

Metric views express joins as plain SQL `ON` clauses; cross-filter direction is implicit. If a DAX measure relied on bidirectional cross-filtering, the metric view will read it as one-directional. Verify totals match before publishing.

## What this skill doesn't try

- **Hidden dimensions / display folders.** All non-hidden columns become dimensions. Trim the YAML manually if you only want a subset exposed.
- **Format strings / data type hints.** DAX format strings (`"#,##0.00"`) are stripped — metric views format at the consumer (BI tool) layer.
- **Calculation groups.** Tabular Editor calculation groups don't have a metric-view analog; rewrite each `CALCULATE`-group selector as an explicit measure.
- **Row-level security (RLS).** DAX RLS rules don't transfer. Use UC `ROW FILTER` policies on the source table (or a wrapper view).
- **Disconnected slicer tables** (e.g. `Period Selection[Index]`-driven SWITCHes). Without a join from the slicer table to the fact, the metric view can't reference its columns. Two options: alias the wrapper measure to its base (parameter applied query-side via WHERE on a real dim), or expose each branch as its own measure and pick one in the consumer.

## DtD / yesterday-snapshot patterns

Snapshot fact tables (one row per BG×Geo×snapshot_date) sometimes need a "value as of yesterday" measure. In DAX:

```dax
DtD Shipment =
CALCULATE(
    SUM('fact_corp_kpi'[qtd_shpmt_net_rev_amt]) / 10^6,
    DATEADD(LASTDATE('D_Calendar'[Date]), -1, DAY))
```

When the original DAX has `DATEADD(..., -1, DAY)` inside a CALCULATE filter, translate it to `is_yesterday_snapshot` and add the flag to the source SQL using `DENSE_RANK()` (robust to weekend/holiday gaps):

```yaml
source: |
  SELECT
    f.*,
    (`data_cutoff_dt` = MAX(`data_cutoff_dt`) OVER ()) AS is_latest_snapshot,
    (DENSE_RANK() OVER (ORDER BY `data_cutoff_dt` DESC) = 2) AS is_yesterday_snapshot
  FROM <fact> f

measures:
  - name: DtD Shipment
    expr: SUM(`qtd_shpmt_net_rev_amt`) FILTER (WHERE is_yesterday_snapshot) / 1000000
```

`DENSE_RANK ... = 2` returns "the snapshot before the most recent one" — last business day for 5×/week feeds. For strict calendar-day -1 substitute `(data_cutoff_dt = MAX(data_cutoff_dt) OVER () - INTERVAL 1 DAY)`.

For arbitrary `DATEADD(date, -N, MONTH/YEAR)` shifts, this shape doesn't generalize — use a `window:` block with `range: trailing N month/year` ordered on a DATE-typed dim (see § Period-over-period patterns above), with `expr: MEASURE(\`Base Measure\`)`.

## When the PBI author commented out the "official" measure above the workaround

A common pattern in mature PBI models:

```dax
Daily Order Load =
//=SUM('fact_corp_kpi'[daily_order_load_net_rev_amt]) / 10^6   -- this is the official measure
VAR OUTPUT = [QTD Order Load_excl.ship] - CALCULATE([QTD Order Load_excl.ship], DATEADD(D_Calendar[Date], -1, DAY))
...
```

The `// = official measure` line is the simple definition the author wants; the multi-VAR `DATEADD` workaround was added because of an upstream data-lag issue still being fixed. **Prefer the commented official version** when you see this pattern. The migrated metric view will be cleaner *and* will become correct automatically when the upstream data is fixed.

## Period-Selection slicer dispatch — expose filter columns as dims, not as SWITCH

PBI models often have a `Period Selection` parameter table joined to the fact with `Index` ∈ {1, 2, 3}, and `*_Modified` measures of this shape:

```dax
Backlog Revenue Modified =
SWITCH(TRUE(),
    MAX('Period Selection'[Index]) = 2,
        CALCULATE([Backlog Revenue], FILTER(D_Calendar, D_Calendar[Last_20days] = "Last 20 Days")),
    MAX('Period Selection'[Index]) = 1,
        CALCULATE([Backlog Revenue], FILTER(D_Calendar, D_Calendar[Last_3days] = "Last 3 Days")),
    MAX('Period Selection'[Index]) = 3, [Backlog Revenue],
    [Backlog Revenue])
```

The DAX dispatches the same base measure with three different date filters, picking which one to evaluate based on a hidden parameter slicer. SQL has no slicer concept; metric views can't route a `SWITCH` over a disconnected-table value into different `FILTER (WHERE …)` clauses.

**Migration:** drop the `*_Modified` wrapper. Expose the filter columns (`Last_20days`, `Last_3days`, …) as **dimensions** on the metric view; the user replicates the dispatch with a query-time `WHERE`:

```yaml
dimensions:
  - name: Last_20days
    expr: d_calendar.`last_20days`
  - name: Last_3days
    expr: d_calendar.`last_3days`
measures:
  - name: Backlog Revenue
    expr: SUM(`qtd_backlog_net_rev_amt`) FILTER (WHERE `bg_geo_date` <> 'ISGPRC1') / 1000000
```

Then to reproduce "Modified, Index=2":

```sql
SELECT MEASURE(`Backlog Revenue`) FROM <view> WHERE `Last_20days` = 'Last 20 Days'
```

If you find yourself writing `CASE WHEN MAX(\`index\`) = 2 THEN ...` in a measure expression, stop — that's a slicer concept that doesn't exist in SQL.

## Multi-fact AUR — separate metric view per fact source

DAX measures sometimes pull revenue from one fact and units from another:

```dax
QTD Order Load AUR =
DIVIDE(
    CALCULATE(SUM('fact_corp_kpi'[qtd_order_load_net_rev_amt]), ...),
    CALCULATE(SUM('cam_ms_order_load'[current_qtd_order_load_qty]), ...))
```

A metric view has ONE `source:` table — it can't aggregate two unrelated facts in one expression. Two migration options:

1. **Pre-join in the source SQL** — if the facts share a key (e.g. `bg_geo_date`), `LEFT JOIN` them in the `source:` block and treat both as columns of one combined fact. Works only when cardinality is 1:1 by snapshot key.

2. **Two metric views, query-time JOIN** — one metric view per fact (`fact_revenue_metrics`, `fact_units_metrics`); the consumer joins MEASURE() outputs in a wrapper query. Loses single-MV governance but keeps each definition clean.

Don't bring the second fact in as a `joins:` entry to compute the AUR — joins are evaluated at GROUP BY time, not aggregate time, so the math will silently double-count.

## Tips

- After running the scaffolder, search the YAML for `AGENT_TRANSLATE_DAX` and `AGENT_AUTHOR` — every occurrence is a placeholder the agent must fill before deployment.
- Use `--fact-table` if the auto-pick (table with most measures) chooses the wrong table.
- Use `--emit-verify-sql` to append a commented-out `SELECT MEASURE(\`X\`) FROM view LIMIT 1` block; uncomment after filling in expressions, then run via `execute_sql` for a live compile-check.
- If a STRING column is used numerically in DAX (revenue stored as text — happens more than you'd think), either fix the source column on ingest, or wrap it in `try_cast(... AS DOUBLE) AS col_num` inside the metric view's `source:` SQL and point measure exprs at the new column.
