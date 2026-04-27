---
name: converting-powerbi-dax-to-metric-views-llm
description: Use when migrating a Power BI semantic model to Databricks Unity Catalog tables and metric views. Reads .pbit (Power BI Template) files directly — no third-party deps, no .pbix-format guessing. Defaults to Kimball snake_case (dim_*/fact_*) and emits schema-only DDL + a metric-view YAML scaffold. The agent (LLM) translates each DAX measure to metric-view SQL using its own knowledge plus references/dax-to-sql-patterns.md; the script handles the mechanical scaffolding (parse, joins, dims, kimball renames, synonyms).
---

# Converting Power BI DAX to Databricks Tables + Metric Views

## Overview

Migrate the structure of a Power BI tabular model into Databricks UC: physical table DDL + a UC metric view on top.

**This is a hybrid skill: the script scaffolds, the agent translates.**

The bundled `dax_to_metric_view.py` parses a **`.pbit`** (Power BI Template — a ZIP containing the full TMSL JSON), picks a fact table, builds star-schema joins from active relationships, and emits a runnable SQL file with:

- Kimball-renamed `CREATE TABLE` DDL (one per real table, schema-only)
- A `CREATE OR REPLACE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$` SCAFFOLD where:
  - `source`, `joins`, `dimensions` are fully populated mechanically
  - `synonyms:` are populated mechanically on every measure and dimension (1:1 PBI bracket / qualified form, deduped against `name:`)
  - **Each measure is a placeholder**: original DAX preserved as YAML comments, `expr: AGENT_TRANSLATE_DAX`, `comment: AGENT_AUTHOR`

The **agent applying this skill (LLM) does the actual DAX→SQL translation**: reads each measure's preserved DAX, translates to metric-view SQL using `references/dax-to-sql-patterns.md` as the cookbook, LLM-authors the comment, and replaces the placeholders. Then the agent optionally uncomments the appended verify SQL block and runs it via `execute_sql` to live-compile-check every measure.

This split is intentional. The script handles what regex does well (parsing, naming, scaffolding); the LLM handles what regex does poorly (DAX semantics, time intelligence, business meaning). The result: 100% of measures land with LLM-quality translation + comments, no `# TODO manual review` rows left behind.

## Three non-negotiable defaults

**1. Kimball naming, snake_case** — `--style kimball` (default).
Tables: lowercase, `dim_*` prefix on every dim, `fact_*` prefix on the fact table. Columns: lowercase snake_case, no spaces or special chars. This is what Genie / dbt / Fabric / AI-generated SQL all expect. No Delta `columnMapping.mode` needed.
Use `--style fidelity` to preserve original PBI names (case + spaces + slashes) — but that requires column mapping mode and is harder for Genie/LLMs to query.

**2. Schema-only output. Do NOT auto-load data.**
The script emits CREATE TABLE DDL + the metric view CREATE VIEW. It never emits `INSERT INTO`. Data ingestion is a separate, explicit step (Lakeflow Connect, PBI Desktop → Export CSV → `COPY INTO`, custom Spark job). If the user asks for "convert to tables and metric views", that is the structure — don't load source data unless they explicitly say "load" / "populate" / "import the data".

**3. Translate every measure — the agent does NOT skip placeholders.**
Every `AGENT_TRANSLATE_DAX` and `AGENT_AUTHOR` placeholder in the scaffold MUST be filled by the agent before deployment. The "Constructs needing extra care during translation" table below lists DAX patterns that require judgment (DATEADD, USERELATIONSHIP, ALL, EARLIER, RANKX). For each: walk through `references/dax-to-sql-patterns.md` and apply the pattern — don't punt unless the construct is genuinely UI-only (see "What NOT to migrate"). Hand-fixed time-intel measures MUST use `MEASURE()` refs to base measures, not re-inlined SUMs:
```yaml
# WRONG — re-inlined SUMs in a windowed measure (breaks single-source-of-truth)
- name: COGS SPLY
  expr: "SUM(`material_costs`)+SUM(`labor_costs_variable`)+SUM(`taxes`)+..."
  window: [{order: Year Start, range: trailing 1 year, semiadditive: last}]

# RIGHT — references the existing Total COGS measure
- name: COGS SPLY
  expr: "MEASURE(`Total COGS`)"
  window: [{order: Year Start, range: trailing 1 year, semiadditive: last}]
```
The window engine resolves the referenced measure's aggregate first, then applies the window. Re-inlining SUMs would force editing every COGS-component change in N places (1 base + N windows) instead of 1.

## Metric view YAML primer

Self-contained reference for the YAML inside `CREATE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$`. Sufficient for everything the scaffolder emits and every translation the agent writes.

### Prerequisites
- Databricks Runtime **17.2+**. **Always use YAML `version: "1.1"`** — for everything (dimensions, measures, window measures alike).
- On DBR < 17.2, dim `comment:`/`synonyms:` aren't recognized by the older serde — re-run the script with `--no-dim-metadata` to strip them. Measures keep full metadata regardless.
- SQL warehouse with `CAN USE`; `SELECT` on source tables; `CREATE TABLE`/`USE SCHEMA` in the target schema.

### Top-level structure

```yaml
version: "1.1"               # Always 1.1
source: catalog.schema.table # OR a `source: |` SQL block (multi-line scalar)
comment: "Description"       # Optional
filter: "col > 0"            # Optional global WHERE applied to every query
joins: [ ... ]               # Optional star/snowflake; see Joins
dimensions: [ ... ]          # Required, ≥1 entry
measures: [ ... ]            # Required, ≥1 entry
```

### Source augmentation (snapshot flags)

The scaffolder emits `source: <fq.table>` by default. **The agent must replace it with an inline SELECT when any measure needs snapshot flags** — i.e. the original DAX contains `LASTDATE(...)`, `FIRSTDATE(...)`, or `DATEADD(..., -1, DAY)` as a CALCULATE filter.

The standard pattern (use the fact's date column in the bracketed places):

```yaml
source: |
  SELECT
    f.*,
    (`data_cutoff_dt` = MAX(`data_cutoff_dt`) OVER ()) AS is_latest_snapshot,
    (`data_cutoff_dt` = MIN(`data_cutoff_dt`) OVER ()) AS is_first_snapshot,
    (DENSE_RANK() OVER (ORDER BY `data_cutoff_dt` DESC) = 2) AS is_yesterday_snapshot
  FROM main.sales.fact_revenue f
```

`DENSE_RANK = 2` (not `MAX - 1 day`) handles snapshot tables with daily writes that have gaps (weekends, holidays).

The script writes the fact's detected date column as a YAML comment hint (`# Fact date column hint (use for snapshot flags if needed): \`data_cutoff_dt\``) above the measures section to remind the agent.

### Joins, dimensions, measures, window measures

See `references/dax-to-sql-patterns.md` for full patterns and examples — that file is the agent's translation cookbook. The fields each object accepts:

```yaml
joins:
  - name: cal                      # alias used in dim/measure exprs
    source: main.sales.dim_calendar
    on: source.`date_id` = cal.`date_id`

dimensions:
  - name: Order Month              # backtick-quoted in queries
    expr: DATE_TRUNC('MONTH', `order_date`)
    comment: "Month bucket of the order."  # business meaning
    synonyms: ["Month", "Order Month bucket"]

measures:
  - name: Total Revenue
    expr: SUM(`total_price`)       # must be an aggregate
    comment: "Sum of order line totals, in dollars."
    synonyms: ["[Total Revenue]"]
  - name: Revenue YTD              # window measure
    expr: MEASURE(`Total Revenue`) # MUST reference the base measure, never re-inline SUM()
    window:
      - order: Order Month
        range: cumulative          # cumulative | trailing N {day,month,year} | current
        semiadditive: last         # first | last
```

## Inputs — `.pbit` only

**The script reads `.pbit` (Power BI Template).** A `.pbit` is a ZIP that includes a `DataModelSchema` JSON file containing the **full** TMSL: every base column, every calculated column, every measure (DAX preserved verbatim), every relationship, with types — exactly what PBI Desktop sees. No third-party library needed; the parser uses only the Python stdlib.

**Why not `.pbix`?** A `.pbix` stores the model in an XPRESS9-compressed Analysis Services backup binary. Community Python readers (pbixray and friends) reverse-engineer it incompletely — base columns from M-loaded tables (e.g., the `Date` PK on a typical `D_Calendar`) silently disappear. `.pbit` exposes the same metadata as plain JSON, so what you see is what gets scaffolded.

**Getting a `.pbit` from a `.pbix`:** Open the `.pbix` in Power BI Desktop → **File → Export → Power BI Template (`.pbit`)**. One click. See `references/extracting-pbi-models.md` for other source formats.

## Workflow

1. **Get the `.pbit`** (export from PBI Desktop — see Inputs above).

2. **Run the scaffolder**:
   ```bash
   python3 scripts/dax_to_metric_view.py model.pbit \
     --catalog-schema main.sales \
     --emit-ddl \
     --fact-suffix profitability \
     --verify \
     --emit-verify-sql \
     --out main_sales.sql
   ```
   Produces:
   - `CREATE OR REPLACE TABLE main.sales.dim_*` (one per non-fact table) and `main.sales.fact_profitability` for the fact
   - `CREATE OR REPLACE VIEW main.sales.fact_profitability_metrics WITH METRICS LANGUAGE YAML AS $$ ... $$` — a SCAFFOLD with measure exprs as `AGENT_TRANSLATE_DAX` and comments as `AGENT_AUTHOR`
   - A verification SQL block (commented out) — one `SELECT MEASURE(\`X\`) FROM view LIMIT 1` per measure

3. **Create the schema** (the script doesn't):
   ```sql
   CREATE SCHEMA IF NOT EXISTS main.sales COMMENT 'PBI migration: ...';
   ```

4. **Translate every measure.** For each `# === Measure i/N: …` block in the scaffold:
   - Read the preserved original DAX (in the `# Original DAX:` block)
   - Translate to metric-view SQL using `references/dax-to-sql-patterns.md` as the cookbook + your own DAX knowledge. Most patterns are mechanical — `SUM('T'[col])` → `SUM(\`col\`)`; `CALCULATE(SUM, 'T'[c]=v)` → `SUM(...) FILTER (WHERE \`c\` = v)`; `IF` → `CASE WHEN … THEN … ELSE … END`. The "Constructs needing extra care" table below lists the patterns that require judgment.
   - LLM-author a one-line `comment:` capturing business meaning (units, scope, snapshot semantics) — see § Synonyms (auto) and comments (LLM-authored) below for examples
   - Replace `expr: AGENT_TRANSLATE_DAX` with your translation, `comment: AGENT_AUTHOR` with your comment
   - Same for dimensions: replace each `# comment: AGENT_AUTHOR …` with a one-line LLM-authored business-meaning comment

5. **Augment `source:` with snapshot flags** if any measure needs `is_latest_snapshot` / `is_first_snapshot` / `is_yesterday_snapshot`. Standard pattern is in § Source augmentation above. Look for these triggers in the original DAX:
   - `LASTDATE(...)` as CALCULATE filter → `is_latest_snapshot`
   - `FIRSTDATE(...)` as CALCULATE filter → `is_first_snapshot`
   - `DATEADD(LASTDATE(...), -1, DAY)` as CALCULATE filter → `is_yesterday_snapshot`

6. **Run the SQL** via `execute_sql` against the workspace. Schema → DDL → CREATE VIEW.

7. **Live compile-check.** Uncomment the verify SQL block at the bottom of the file (one `SELECT MEASURE(\`X\`) FROM view LIMIT 1` per measure) and run via `execute_sql`. Catches syntax errors, type mismatches, `MEASURE() FILTER` rejections, `BINARY_OP_DIFF_TYPES` on bad window-order types — anything the static check can't see.

8. **Wire data ingestion as a separate step.** The tables are empty. Use Lakeflow Connect, PBI Desktop → Export → CSV → `COPY INTO`, or a Spark job.

9. **Spot-check totals.** Once data is loaded, run `MEASURE(\`Total Revenue\`)` queries against the metric view and compare with the same measure in Power BI on a sample slice. Bidirectional cross-filtering and time intelligence are the usual divergence points.

## Flags

| Flag | What it controls |
|------|------------------|
| `--style kimball` (default) | Tables: `dim_*`, `fact_*`. Columns: snake_case lowercase. No Delta column mapping. |
| `--style fidelity` | Tables: original PBI names. Columns: original (with spaces). DDL emits Delta `columnMapping.mode='name'` automatically. |
| `--catalog-schema CATALOG.SCHEMA` | Three-level prefix for table refs in joins and CREATE statements. Required for runnable SQL. |
| `--source CATALOG.SCHEMA.FACT` | Override the metric-view's `source:` (also accepts a SQL block — useful for adding LAG cols by hand). |
| `--fact-table NAME` | Pick a specific PBI table as the fact (default: most measures). |
| `--fact-suffix WORD` | Domain suffix for kimball fact-table name. `--fact-suffix profitability` → `fact_profitability`. |
| `--emit-ddl` | Emit CREATE TABLE DDL alongside the metric view (default off — emits only the view). |
| `--verify` | Run static schema check (dimensions only — measure exprs are placeholders) + structural diff. |
| `--emit-verify-sql` | Append a verification SQL block (commented out by default — agent uncomments after filling in expressions). |
| `--no-dim-metadata` | Strip `comment:` and `synonyms:` from every dimension. Use this if your workspace runs a metric-view serde older than v1.1 (DBR < 17.2) — those parsers know only `name/expr/window` on dims and reject the YAML with `Unrecognized field 'synonyms' (class … v10.Column)`. Measures keep full metadata. |

## Verification levels

The skill catches different bug classes at different points:

| Level | Catches | When | How |
|---|---|---|---|
| 1. **Helper unit tests** | Synonym / kimball-rename / YAML-quote bugs | Before commit | `python3 scripts/test_translation.py` |
| 2. **Static schema check** | Dimension column ref typos, alias mismatches | `--verify`, no DB needed | walks every dim expr, resolves backticks against source/joined columns. (Measure exprs are placeholders at scaffold time — Level 4 catches those.) |
| 3. **Structural diff** | Lost or extra tables / measures / joins between PBIT and YAML | `--verify`, no DB needed | counts source PBIT vs scaffolded metric view |
| 4. **Live compile check** | Syntax errors, type mismatches, `MEASURE() FILTER` rejections, anything the static check can't see | `--emit-verify-sql` then run via `execute_sql` (after agent fills in exprs) | `SELECT MEASURE(\`X\`) FROM view LIMIT 1` per measure — fails fast on any expression that doesn't compile |
| 5. **Translation correctness** | Wrong SQL semantics (right syntax, wrong meaning) | Manual review | spot-check against the cookbook + sample queries against PBI |
| 6. **Numerical equivalence** | DAX vs SQL produce same numbers | Manual | sample queries in PBI Desktop vs Databricks, compare CSVs |

## Synonyms (auto) and comments (LLM-authored)

The scaffolder emits two distinct fields per **measure AND dimension** for documentation and discovery. Both objects get the same treatment — Genie/AI/BI search hits dimensions and measures equally, so dropping metadata on either side leaves a discovery hole.

### `synonyms:` — auto, mechanical, deduplicated against `name:`

#### Measures

Every emitted measure carries a `synonyms:` list with the original PBI measure name as a DAX bracket-form lookup. **Bare-name forms equal to `name:` are dropped** (would be redundant — Genie matches `name:` directly).

The rule:

- **No rename** (migrated `name:` equals the original PBI name): emit only the bracket form.
  ```yaml
  - name: Last Q AUR Actual
    synonyms:
      - "[Last Q AUR Actual]"  # bracket form only — bare name dedups against name:
  ```

- **Renamed** (migrated `name:` differs because of `%`/`/` etc. that aren't valid in metric-view names): emit both forms — the original bracket form AND the original bare form.
  ```yaml
  - name: QTD Order Load Pct ASP   # renamed: % → Pct
    synonyms:
      - "[QTD Order Load % ASP]"   # canonical DAX bracket form
      - "QTD Order Load % ASP"     # original bare name (differs from name:, so kept)
  ```

#### Dimensions

Every emitted dimension carries a `synonyms:` list. PBI columns are referenced in DAX as `'Table'[Column]` (qualified) — that's what the scaffolder emits, alongside the bare column name. Same dedup rule: anything matching `name:` is dropped.

```yaml
- name: D_Calendar Date
  expr: d_calendar.`date`
  synonyms:
    - Date                        # bare PBI col name
    - "'D_Calendar'[Date]"        # canonical DAX qualified form
```

Databricks metric views accept `synonyms:` per **measure and dimension** as a list. Genie / AI/BI consumers use it to resolve user queries to the right object when the migrated `name:` differs from what was typed.

**DBR version note:** Measure `synonyms:`/`comment:` work on every recent DBR. **Dimension** `synonyms:`/`comment:` require the **v1.1 metric-view serde (DBR 17.2+)**; older runtimes only know `name/expr/window` on dims and reject the YAML with `Unrecognized field 'synonyms' …`. If you hit that error, either upgrade DBR or re-run with `--no-dim-metadata`.

### `comment:` — LLM-authored by the agent

Comments are **the agent's responsibility**. The scaffolder:

- Preserves the source PBI `description` field verbatim if present (human-authored documentation always wins) — applies to both measures and dimensions.
- Emits an `AGENT_AUTHOR` placeholder otherwise — the agent fills it.

Why not rule-based: regex pattern detection over DAX or column names produces shallow, generic, often misleading text ("Ratio of x to expr", "Sum of qtd_shpmt at yesterday's snapshot", "The bg_cd column") that's worse than no comment. Once a misleading comment lands in UC, downstream tools (Genie, LLM agents indexing the catalog) treat it as ground truth.

**Good agent comments** are short, factual, and business-meaningful:

For measures:
- `Target ASP for the latest snapshot, in millions of dollars.` ✓
- `Total revenue at the latest data cutoff, in millions; excludes column-flag-1 rows.` ✓
- `Days remaining in the current quarter (≥1 to avoid /0 in pacing calculations).` ✓
- `Sum of qtd_shpmt_net_rev_amt at yesterday's snapshot.` ✗ (shallow rule-based — describes the SQL, not the business)

For dimensions:
- `Fiscal year string (e.g. "FY24"). Aligns with the company's fiscal calendar, not calendar year.` ✓
- `Snapshot timestamp — the data-cutoff datetime each daily snapshot was taken at.` ✓
- `Composite BG×Geo×Date marker; the value 'ISGPRC1' identifies the ISG/PRC bucket whose order_load is unreliable and is substituted with shipment.` ✓
- `The bg_cd column.` ✗ (echoes the column name; says nothing)
- `String column.` ✗ (type info — not business meaning)

## Translation cookbook (agent uses these)

The agent applies these patterns when filling in `AGENT_TRANSLATE_DAX` placeholders. For the full reference (mechanism details, period-over-period strategies, JOIN-CTE migration shape) see `references/dax-to-sql-patterns.md`.

| DAX | Metric view SQL |
|-----|-----------------|
| `SUM/AVERAGE/MIN/MAX/COUNT('T'[c])` | `SUM/AVG/MIN/MAX/COUNT(\`c\`)` |
| `COUNTA('T'[c])` | `COUNT(\`c\`)` |
| `COUNTROWS('T')` | `COUNT(1)` |
| `DISTINCTCOUNT('T'[c])` | `COUNT(DISTINCT \`c\`)` |
| `CALCULATE(agg, 'T'[col]=val)` | `agg FILTER (WHERE \`col\` = val)` |
| `CALCULATE(agg, FILTER('T', expr))` | `agg FILTER (WHERE expr)` (FILTER table-arg unwrapped) |
| `CALCULATE(agg, LASTDATE('Cal'[Date]))` | `agg FILTER (WHERE is_latest_snapshot)` — **and** augment `source:` with the flag column (see § Source augmentation) |
| `CALCULATE(agg, FIRSTDATE('Cal'[Date]))` | `agg FILTER (WHERE is_first_snapshot)` (same shape) |
| `CALCULATE(agg, DATEADD(LASTDATE('Cal'[Date]), -1, DAY))` | `agg FILTER (WHERE is_yesterday_snapshot)` — augment `source:` with `DENSE_RANK() OVER (ORDER BY date DESC) = 2` |
| `CALCULATE(SUM(x)/10^6, p)` | `SUM(x) FILTER (WHERE p) / 1000000` — FILTER attaches to the aggregate, NOT after the / division |
| `DIVIDE(a, b)` | `(a) / NULLIF((b), 0)` |
| `IFERROR(a, b)` | `a` (NULLIF in DIVIDE handles /0 — the typical IFERROR use) |
| `IF(c, a, b)` / `IF(c, a)` | `CASE WHEN c THEN a ELSE b END` / `CASE WHEN c THEN a END` |
| `IF(SELECTEDVALUE(...)='X', "-", real)` | unwrap to `real` (UI edge-case display drop). Same for compound `&&`/`NOT … IN {…}` slicer guards. |
| `IF(<expr>=BLANK(), "-", <expr>)` / `IF(<expr><>BLANK(), <expr>, "-")` | unwrap to `<expr>`. Pure UI fallback. |
| `SWITCH(x, v1, r1, ..., default)` | `CASE WHEN x = v1 THEN r1 ... ELSE default END` |
| `SWITCH(TRUE(), c1, r1, ..., default)` | `CASE WHEN c1 THEN r1 ... ELSE default END` (boolean-conditioned) |
| `BLANK()` | `NULL` |
| `TRUE()` / `FALSE()` | `TRUE` / `FALSE` |
| `&&` / `\|\|` | `AND` / `OR` |
| `&` (string concat) | `\|\|` |
| `^` (`10^6`) | `1000000` |
| `FORMAT(x, "fmt")` | `x` (format strings are consumer-side) |
| `CONCATENATE(a, b)` | `concat(a, b)` |
| `'T'[col]` | `` `col` `` (or `joinalias.\`col\`` if it's a joined-table column) |
| `[Measure Name]` | `MEASURE(\`Measure Name\`)` if that name is declared in the YAML; else `` `Measure Name` `` (column on fact) |
| `VAR x = e1 VAR y = e2 ... RETURN body` | inline bindings into body — substitute every reference to `<name>` with `(<expr>)`; recursive for nested VARs |

## Constructs needing extra care during translation

These DAX patterns require judgment beyond the cookbook table. The agent should pause and pick the right shape rather than mechanical-translate.

| Construct | Why | Suggested rewrite |
|-----------|-----|-------------------|
| `DATEADD(..., -N, MONTH/YEAR)`, `TOTALYTD`, `SAMEPERIODLASTYEAR`, `PARALLELPERIOD`, `PREVIOUS{MONTH,QUARTER,YEAR}` | Time-shift requires a window measure or a SQL source with `LAG()`. | Add a `window:` block with `range: trailing N month/year` (or `cumulative` for YTD) ordered on a date dim. **`expr:` MUST be `MEASURE(\`Base Measure\`)`, never a re-inlined `SUM(...)`** — see non-negotiable default #3. Alternative: augment `source:` with `LAG(...)` columns. See `references/dax-to-sql-patterns.md` § Period-over-period patterns. (`DATEADD(LASTDATE, -1, DAY)` is the snapshot-flag case — translate to `is_yesterday_snapshot` per § Source augmentation.) |
| `ALL('geo')`, `ALLEXCEPT`, `ALLSELECTED` used to compute a "WW total" override | Drops filter context — no SQL analog. | Bake each unfiltered total into a separate measure (e.g. `Revenue WW`, `Revenue ISO`); the user picks the one they want at query time. Don't try to encode "if slicer=X show WW else show local" — it's a UI display rule. |
| `USERELATIONSHIP` | Activates an inactive relationship. | Add a second `joins:` entry; pick which to filter at query time. |
| `EARLIER`, `EARLIEST`, `RANKX`, `TOPN` | Row-context evaluation. | Rewrite as a window function in the SQL `source:` or a wrapper view. |
| `LOOKUPVALUE('Dim'[col], 'Dim'[key], <key_expr>)` | Star-schema lookup with row-context. | Reference the joined table directly: `dim.col`. The rewrite is mechanical when the join already exists. |
| `RELATED('Dim'[col])` | Star-schema lookup. | Reference the joined table directly: `dim.col`. |
| Outer `SELECTEDVALUE/ISFILTERED` not in an `IF(..., "-", real)` wrapper (e.g. dispatching like `IF(SELECTEDVALUE(BG)="PCSD", PCSD_Units, StandardUnits)`) | Generic slicer-context manipulation. | Drop the slicer-conditional branch and ship the simpler "real" calculation. Or split into 2 measures (`Units PCSD` and `Units Standard`); user picks at query time. Last resort: expose the slicer column as a query-time WHERE on a dimension. |
| `SUM(...)+SUM(...)` constructed across two separate fact tables | Cross-fact arithmetic; metric view has one `source` | Build a separate metric view per fact source. Or, if joinable on PK, expose one as a `joins:` entry and reference its column. |

## What NOT to migrate (skip these)

These are intentionally UI-only or non-aggregable — they aren't real KPIs and shouldn't be carried into the metric view. Replace `expr: AGENT_TRANSLATE_DAX` with a placeholder note explaining the skip, and remove the measure entry from the YAML before deploying.

| Pattern | What it is in PBI | Why skip |
|---------|-------------------|----------|
| `Test_*`, `*_Format`, parameter-table SWITCH-on-index measures | DAX measures that route to a "selected" measure based on `SELECTEDVALUE('Param'[Index])` from a hidden parameter table — drives buttons/toggles in the Power BI visual. | Pure visual dispatch; no SQL analog. The underlying measures are already migrated; users select them by name. |
| Color/format string measures (`'#77B947'`, `'#DE6A73'`) | Conditional formatting expressions returning hex color codes. | Not numeric. Conditional formatting belongs on the consumer (AI/BI dashboard, Power BI). |
| `"Cutoff Date up to " & LASTDATE(...)` and similar string-display measures | Title/header label measures. | Not aggregable. Use a SQL view or compute at query time. |
| `*_Modified` family with `SWITCH(TRUE(), max('Period Selection'[Index])=2, CALCULATE(<base>, Last_20days = "Last 20 Days"), …)` | Period-Selection slicer dispatch — dispatches the base measure with a `Last_20days`/`Last_3days` filter. | Replace with: expose `Last_20days`, `Last_3days` (whatever filter columns the SWITCH dispatches on) as **dimensions** on the metric view. The user replicates the modified behavior with a query-time `WHERE Last_20days = 'Last 20 Days'`. Don't try to encode the SWITCH. |

## Quick reference — common follow-up edits

| Symptom | Fix |
|---------|-----|
| `<TODO_CATALOG.SCHEMA>.X` still in YAML | You forgot `--catalog-schema`. Re-run with it. |
| `AGENT_TRANSLATE_DAX` still in deployed YAML | Agent didn't fill in a measure. Re-translate it. The `--emit-verify-sql` output catches this on live compile. |
| Wrong fact table picked (too few measures show up) | Re-run with `--fact-table TableName`. |
| Dim columns leaked as dimensions but you want fewer | Delete those entries from `dimensions:`. |
| `BINARY_OP_DIFF_TYPES` on `INTERVAL '-1' YEAR` | The window's `order` dim is INT — change it to `DATE_TRUNC('YEAR', date)`. |
| `MEASURE(\`X\`) FILTER (WHERE ...)` rejected by engine | `MEASURE()` doesn't support FILTER clause. Inline the underlying agg expression instead. |
| Aggregating a STRING column (e.g. revenue stored as text) | The DDL emits the original type. Either fix the DDL/cast on ingest, or wrap the column in `try_cast(... AS DOUBLE)` inside the SQL `source:`. |
| `[METRIC_VIEW_INVALID_VIEW_DEFINITION] Unrecognized field "synonyms" (class … v10.Column)` | Your DBR is on a metric-view serde older than v1.1 (DBR < 17.2). Re-run with `--no-dim-metadata`. (Measures are unaffected.) Or upgrade DBR. |

## Files

- `scripts/dax_to_metric_view.py` — scaffolder (Python stdlib only; reads `.pbit`, emits DDL + YAML scaffold with `AGENT_TRANSLATE_DAX` placeholders).
- `scripts/test_translation.py` — unit tests for the helper functions (kimball naming, synonyms, YAML quoting, auto-date filter). No `.pbit` needed.
- `references/dax-to-sql-patterns.md` — translation cookbook the agent uses to fill in measure expressions. Covers CALCULATE→FILTER unwrap, snapshot-flag patterns, VAR/RETURN inlining, period-over-period strategies, JOIN-CTEs migration shape.
- `references/extracting-pbi-models.md` — getting various PBI sources to a `.pbit`.

## Common mistakes

- **Trying to feed a `.pbix` directly.** The script rejects `.pbix`. Export to `.pbit` from PBI Desktop first — that takes 1 click and gives you the full schema as JSON. Working around with pbixray silently drops base columns from M-loaded tables.
- **PBI auto date/time hidden tables.** When **Auto date/time** is enabled in Power BI Desktop (the default), PBI silently generates one `DateTableTemplate_<GUID>` plus one `LocalDateTable_<GUID>` per Date column to drive the auto Year→Quarter→Month→Day hierarchy. They contain no real business data. The script excludes them from DDL emission, joins, dimensions, and fact-table picking via the `_AUTO_DATE_PREFIXES` filter.
- **Auto-loading source data.** Don't. "Convert to tables" means structure. Data ingestion is a separate explicit step.
- **Defaulting to fidelity style without being asked.** Genie / AI / dbt expect Kimball snake_case.
- **Trusting bidirectional cross-filter behavior.** Power BI's bidirectional filters silently change measure totals. Metric view joins are one-directional SQL `ON`. Sample-check totals against PBI before publishing.
- **Forgetting `--catalog-schema`.** Without it, the YAML has `<TODO_CATALOG.SCHEMA>.<FactName>` placeholders and the SQL won't run.
- **Deploying with `AGENT_TRANSLATE_DAX` placeholders still in the YAML.** Won't compile. Run the live compile-check (`--emit-verify-sql` block) after filling in expressions to catch any you missed.
- **Forgetting to augment `source:` with snapshot flags.** When measures use `LASTDATE` / `FIRSTDATE` / `DATEADD(...,-1,DAY)` as CALCULATE filters, the agent must replace `source:` with the inline-SELECT pattern (see § Source augmentation). The script flags the fact's date column hint at the top of the measures section to make this easy.
- **Ignoring the PBI author's commented-out "official" measure.** When a measure starts with `// =SUM(…) / 10^6 this is the official measure` followed by a workaround using `DATEADD`/`LASTDATE`/`CALCULATE`, prefer the *commented* official definition. The workaround is a band-aid for an upstream data lag the PBI author is waiting to fix; the simple commented version is what they intend the measure to be.
- **Putting `FILTER` after the `/ N` division.** SQL `FILTER (WHERE …)` is a clause on the aggregate, not on the surrounding arithmetic. `SUM(x) FILTER (WHERE p) / 1000000` ✓ — `SUM(x) / 1000000 FILTER (WHERE p)` ✗ (parse error).
- **Wrapping `MEASURE()` in `FILTER (WHERE …)`.** The engine rejects this. Inline the underlying aggregate expression instead.
- **Treating inactive relationships as auto-translated.** The scaffolder skips relationships where `isActive=False` and warns. If a measure depends on `USERELATIONSHIP`, wire the join manually.
- **Inlining raw `SUM(...)` in windowed measures instead of `MEASURE()` refs.** Already covered as non-negotiable default #3 — *do not* re-inline. If you find yourself typing `SUM(\`material_costs\`)+SUM(\`labor_costs_variable\`)+...` in a `window:` block, stop and replace with `MEASURE(\`Total COGS\`)`. The window engine resolves the referenced measure's aggregate first, then applies the window.
- **Forgetting that `SUM('T'[col])` on a STRING column won't work.** If the source column is text but the DAX treats it numerically, either fix the source column type (DDL/ingest) or `try_cast` it inside the metric view's `source:` SQL.
