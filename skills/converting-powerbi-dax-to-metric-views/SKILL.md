---
name: converting-powerbi-dax-to-metric-views
description: Use when migrating a Power BI semantic model to Databricks Unity Catalog tables and metric views. Reads .pbit (Power BI Template) files directly — no third-party deps, no .pbix-format guessing. Defaults to Kimball snake_case (dim_*/fact_*) and emits schema-only DDL + the metric view DDL. Translates a wide DAX surface (SUM, AVERAGE, MIN, MAX, COUNT, COUNTROWS, DISTINCTCOUNT, CALCULATE, FILTER('T',expr), DIVIDE, IF, SWITCH, SWITCH(TRUE(),...), VAR/RETURN inlining, IFERROR, BLANK, FORMAT, &/||, ^, LASTDATE/FIRSTDATE → is_latest_snapshot flag, SELECTEDVALUE/ISFILTERED IF-wrapper unwrap, bare [Measure] refs, forward-reference detection); flags time-intel (DATEADD, TOTALYTD, SAMEPERIODLASTYEAR) for window-measure rewrite.
---

# Converting Power BI DAX to Databricks Tables + Metric Views

## Overview

Migrate the structure of a Power BI tabular model into Databricks UC: physical table DDL + a UC metric view on top. The bundled converter reads a **`.pbit`** (Power BI Template — a ZIP containing the full TMSL JSON), picks a fact table, builds star-schema joins from active relationships, and translates DAX measures to metric-view SQL. Anything it can't auto-translate (time intelligence, deep filter-context manipulation) is preserved as a YAML comment with the original DAX so a human can finish it.

## Three non-negotiable defaults

**1. Kimball naming, snake_case** — `--style kimball` (default).
Tables: lowercase, `dim_*` prefix on every dim, `fact_*` prefix on the fact table. Columns: lowercase snake_case, no spaces or special chars. This is what Genie / dbt / Fabric / AI-generated SQL all expect. No Delta `columnMapping.mode` needed.
Use `--style fidelity` to preserve original PBI names (case + spaces + slashes) — but that requires column mapping mode and is harder for Genie/LLMs to query.

**2. Schema-only output. Do NOT auto-load data.**
The converter emits CREATE TABLE DDL + the metric view CREATE VIEW. It never emits `INSERT INTO`. Data ingestion is a separate, explicit step (Lakeflow Connect, PBI Desktop → Export CSV → `COPY INTO`, custom Spark job). If the user asks for "convert to tables and metric views", that is the structure — don't load source data unless they explicitly say "load" / "populate" / "import the data".

**3. Translate as much as possible — do NOT be conservative with the dropped-measure list.** The converter's flagged-for-review list is the *floor*, not the *ceiling*, of what needs hand-rewriting. Many flagged measures are mechanical rewrites the table in this skill describes — `IF(SELECTEDVALUE(...) IN {...}, "-", real)` unwraps to `real`; `LOOKUPVALUE('Dim'[col], ...)` becomes `dim.col`; `IF(<expr>=BLANK(), "-", <expr>)` unwraps to `<expr>`. Before deciding a measure can't be migrated, walk through the **Quick reference** patterns and **Flagged for manual review** table below — each row has the rewrite. Acceptable reasons to skip: pure UI dispatch (Test_*, parameter-table SWITCH-on-index), color-string measures for visual conditional formatting, measures whose source is a different fact (split into a second metric view), or mixed cross-fact AUR (revenue from one table / units from another). Everything else: rewrite it.

**4. Hand-fixed time-intel measures MUST use `MEASURE()` refs to base measures, not re-inlined SUMs.** When you replace a flagged TOTALYTD / SAMEPERIODLASTYEAR / DATEADD measure with a `window:` block, the `expr:` MUST reference the base non-windowed measure via `MEASURE(\`Total COGS\`)`, NOT the underlying `SUM(...)+SUM(...)+...` formula. The window engine resolves the referenced measure's aggregate first, then applies the window. Re-inlining SUMs breaks single-source-of-truth: a 6-component COGS definition would have to be edited in 7 places (base + 3 YTD + 3 SPLY) instead of 1. This is a HARD RULE — the only exception is if `MEASURE()` cannot be used (the rare cycle case the converter flags).
```yaml
# WRONG — re-inlined SUMs in a windowed measure (breaks single-source-of-truth)
- name: COGS SPLY
  expr: "SUM(`material_costs`)+SUM(`labor_costs_variable`)+SUM(`taxes`)+SUM(`rev_for_exp_travel`)+SUM(`travel_expenses`)+SUM(`cost_third_party`)"
  window: [{order: Year Start, range: trailing 1 year, semiadditive: last}]

# RIGHT — references the existing Total COGS measure
- name: COGS SPLY
  expr: "MEASURE(`Total COGS`)"
  window: [{order: Year Start, range: trailing 1 year, semiadditive: last}]
```

The converter's output is a directly-runnable SQL file: `CREATE TABLE` × N + one `CREATE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$`. The metric-view YAML format is summarized in **§ Metric view YAML primer** below — that section is the only metric-views reference you need; no separate skill lookup required.

## Metric view YAML primer

Self-contained reference for the YAML inside `CREATE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$`. Sufficient for everything the PBI converter emits and every hand-edit this skill describes.

### Prerequisites
- Databricks Runtime **17.2+**. **Always use YAML `version: "1.1"`** — for everything (dimensions, measures, window measures alike).
- On DBR < 17.2, dim `comment:`/`synonyms:` aren't recognized by the older serde — re-run the converter with `--no-dim-metadata` to strip them. Measures keep full metadata regardless.
- SQL warehouse with `CAN USE`; `SELECT` on source tables; `CREATE TABLE`/`USE SCHEMA` in the target schema.

### Top-level structure

```yaml
version: "1.1"               # Required for v1.1 features (comments on dims/measures)
source: catalog.schema.table # OR a `source: |` SQL block (multi-line scalar)
comment: "Description"       # Optional
filter: "col > 0"            # Optional global WHERE applied to every query
joins: [ ... ]               # Optional star/snowflake; see Joins
dimensions: [ ... ]          # Required, ≥1 entry
measures: [ ... ]            # Required, ≥1 entry
```

The PBI converter emits a `source: |` block when it needs to augment with computed columns (`is_latest_snapshot`, `is_yesterday_snapshot`); otherwise a plain three-level table ref.

### Dimensions

```yaml
dimensions:
  - name: Order Month                                # Backtick-quoted in queries
    expr: "DATE_TRUNC('MONTH', order_date)"          # Any SQL expression
    comment: "Month of order"                        # Optional (v1.1+)
  - name: Region
    expr: "customer.region_name"                     # Reference a joined dim via <join_name>.<col>
  - name: Status Label                               # Multi-line CASE OK
    expr: |
      CASE WHEN status = 'O' THEN 'Open'
           WHEN status = 'F' THEN 'Fulfilled' END
```

Rules: `name` becomes the column users SELECT (backtick-quote if it has spaces / special chars); `expr` cannot use aggregate functions; can reference source columns OR `joinname.col` from any `joins:` entry.

### Measures

```yaml
measures:
  - name: Order Count
    expr: "COUNT(1)"
  - name: Total Revenue
    expr: "SUM(total_price)"
  - name: Open Revenue                               # Filtered aggregate
    expr: "SUM(total_price) FILTER (WHERE status = 'O')"
  - name: Revenue per Customer                       # Ratio is safe to re-aggregate
    expr: "SUM(total_price) / NULLIF(COUNT(DISTINCT customer_id), 0)"
  - name: Open Revenue Pct of Total                  # Compose with MEASURE()
    expr: "MEASURE(`Open Revenue`) / NULLIF(MEASURE(`Total Revenue`), 0)"
```

Rules:
- `expr` MUST contain an aggregate function. `MEASURE(\`X\`)` counts as an aggregate (it resolves to one).
- Queried via `MEASURE(\`name\`)` — `SELECT *` on a metric view is **not supported**.
- `MEASURE()` does NOT support `FILTER (WHERE …)` — engine rejects it. Inline the underlying aggregate instead (`SUM(x) FILTER (WHERE p)`, not `MEASURE(\`X\`) FILTER (WHERE p)`).
- Reference joined-dim columns directly inside aggregates (`SUM(customer.amount)` is fine).

### Joins (star schema)

```yaml
source: catalog.schema.fact_orders
joins:
  - name: customer
    source: catalog.schema.dim_customer
    on: "source.customer_id = customer.id"           # Fact aliased as `source`
  - name: product
    source: catalog.schema.dim_product
    using: [product_id]                              # Or `using:` list as alternative to `on:`
```

### Joins (snowflake, DBR 17.1+)

```yaml
joins:
  - name: customer
    source: catalog.schema.dim_customer
    on: "source.customer_id = customer.id"
    joins:                                           # Nested
      - name: nation
        source: catalog.schema.dim_nation
        on: "customer.nation_id = nation.id"
```

Rules: the fact is always `source` in `on:`; joined tables are referenced by their `name`; nested `joins:` chain via the parent's name; joined tables cannot include MAP type columns. The PBI converter emits a single level of joins (one per relationship that touches the fact); promote to snowflake by hand if the model is multi-hop.

### Window measures (period-over-period, running totals)

Window measures are still experimental but use the standard YAML `version: "1.1"` like everything else. Add a `window:` block to a measure:

```yaml
version: "1.1"
...
measures:
  - name: Total Revenue
    expr: "SUM(total_price)"
  - name: Revenue YTD
    expr: "MEASURE(`Total Revenue`)"                 # MUST reference base measure, never re-inline SUM()
    window:
      - order: "Order Date"                          # Dim that orders the window
        range: cumulative                            # cumulative | current | trailing N <unit> | leading N <unit> | all
        semiadditive: last                           # first | last
  - name: Revenue Trailing 12M
    expr: "MEASURE(`Total Revenue`)"
    window:
      - order: "Order Month"                         # MUST be DATE-typed dim for `<unit>=month/year`
        range: trailing 12 month
        semiadditive: last
```

Window-block fields: `order` (dim name), `range` (`current` / `cumulative` / `trailing N day|week|month|year` / `leading N <unit>` / `all`), `semiadditive` (`first` / `last` — value to use when the order dim isn't in GROUP BY).

Multiple windows can compose on one measure; see § Period-over-period patterns in `references/dax-to-sql-patterns.md`.

### Filter

```yaml
filter: "order_date >= '2024-01-01' AND status != 'CANCELLED'"
```

Applied as a WHERE on every query. Use for permanent business-rule filters (snapshot-day rules belong in `source:` SQL augmentation instead, e.g. `is_latest_snapshot`).

### Querying

```sql
SELECT
  `Order Month`,                                     -- Backtick dim names with spaces
  `Region`,
  MEASURE(`Total Revenue`)        AS revenue,
  MEASURE(`Revenue per Customer`) AS rpc
FROM catalog.schema.orders_metrics
WHERE `Region` = 'EMEA'
GROUP BY ALL
ORDER BY ALL
```

### Common engine errors and fixes

| Error | Fix |
|-------|-----|
| `[INVALID_SQL_SYNTAX.FUNCTION_WITH_UNSUPPORTED_SYNTAX] The function MEASURE does not support FILTER CLAUSE` | Inline the aggregate: `SUM(x) FILTER (WHERE p)` instead of `MEASURE(\`Y\`) FILTER (WHERE p)`. |
| `[PARSE_SYNTAX_ERROR] Syntax error at or near '('` near `… / 1000000 FILTER (WHERE …)` | FILTER attaches to the aggregate, not after `/`: `SUM(x) FILTER (WHERE p) / 1000000`. |
| `Cannot resolve column …` on a name with spaces or `+` | Backtick-quote dimension names: `` `BG+GEO+Date` ``, `` `Order Month` ``. |
| `BINARY_OP_DIFF_TYPES` involving `INTERVAL` | A window's `order:` dim is INT (e.g. `year`); change it to a DATE-typed dim like `DATE_TRUNC('YEAR', date_col)`. |
| `SELECT *` returns "metric view does not support …" | Always list dimensions explicitly and wrap measures in `MEASURE()`. |

## When to use

- A customer wants their Power BI measures available as governed UC metrics queryable from Genie / AI/BI dashboards / DirectQuery.
- You have a `.pbit` and need a starting metric view definition.
- You're auditing how many DAX measures are mechanically convertible vs. need a rewrite (run `--strict` to count).

Don't use for:
- Round-tripping back to Power BI — for that, [Tabular Editor 3 Semantic Bridge](https://tabulareditor.com/product/features/semantic-bridge) is the reverse path.
- Migrating *visuals*, RLS rules, calculation groups, or KPIs — those have no metric-view analog.

## Inputs — `.pbit` only

**The converter reads `.pbit` (Power BI Template).** A `.pbit` is a ZIP that includes a `DataModelSchema` JSON file containing the **full** TMSL: every base column, every calculated column, every measure, every relationship, with types — exactly what PBI Desktop sees. No third-party library needed; the parser uses only the Python stdlib.

**Why not `.pbix`?** A `.pbix` stores the model in an XPRESS9-compressed Analysis Services backup binary. Community Python readers (pbixray and friends) reverse-engineer it incompletely — base columns from M-loaded tables (e.g., the `Date` PK on a typical `D_Calendar`) silently disappear. `.pbit` exposes the same metadata as plain JSON, so what you see is what gets converted.

**Getting a `.pbit` from a `.pbix`:** Open the `.pbix` in Power BI Desktop → **File → Export → Power BI Template (`.pbit`)**. One click. See `references/extracting-pbi-models.md` for other source formats.

## Workflow

1. **Get the `.pbit`** (export from PBI Desktop — see Inputs above).

2. **Run the converter** with the catalog/schema where output should live. Default emits Kimball-style DDL + the metric view CREATE VIEW in one runnable SQL file.
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
   - `CREATE OR REPLACE TABLE main.sales.dim_bu (...)` … one per non-fact table
   - `CREATE OR REPLACE TABLE main.sales.fact_profitability (...)` for the fact
   - `CREATE OR REPLACE VIEW main.sales.fact_profitability_metrics WITH METRICS LANGUAGE YAML AS $$ ... $$`
   - A verification SQL block with one `SELECT MEASURE(\`X\`)` per measure
   stderr lists DAX measures that need manual rewrite (time intelligence, filter-context manipulation, etc.).

3. **Create the schema if it doesn't exist** (skill does not assume it).
   ```sql
   CREATE SCHEMA IF NOT EXISTS main.sales COMMENT 'PBI migration: ...';
   ```

4. **Run the generated SQL** via `execute_sql` against the workspace.

5. **Hand-translate any flagged measures.** Search the SQL for `# TODO manual review` — each flagged measure has its original DAX preserved as a comment. See `references/dax-to-sql-patterns.md` for rewrites, especially the **Period-over-period patterns** section. Checklist for every hand-fixed time-intel measure:
   - [ ] **`expr:` is `MEASURE(\`Base Measure\`)`, NOT a re-inlined SUM/+SUM expression.** (See non-negotiable default #3.)
   - [ ] **`CASE WHEN <numeric> THEN ... END`** patterns the converter emitted from `IF([SPLY], var/SPLY, BLANK())` are NOT valid SQL — `CASE WHEN` requires a boolean. Replace with `MEASURE(\`var\`) / NULLIF(MEASURE(\`SPLY\`), 0)`.
   - [ ] If the measure references columns from a joined dim (e.g., `scenario`, `date`), qualify with the join alias (`scenario.\`scenario\``, not `\`scenario\``).
   - [ ] Always use `version: "1.1"` at the top of the YAML — for window measures and everything else.
   - [ ] If using `range: trailing N year`, the `order:` dim MUST be DATE (not INT) — add a `Year Start` dim as `DATE_TRUNC('YEAR', date.\`date\`)`.

6. **Wire data ingestion as a separate step.** The tables are empty. Use Lakeflow Connect, PBI Desktop → Export → CSV → `COPY INTO`, or a Spark job. Do NOT auto-emit `INSERT INTO` SQL — that's a separate task the user must explicitly request.

7. **For models with multiple PoP measures: also emit a `*_kpi_wide` SQL view.** A single metric-view query cannot return MoM + YoY-by-month + QoQ together (different windows need different GROUP BY shapes — see `references/dax-to-sql-patterns.md` § Period-over-period patterns). The intended migration shape is a multi-CTE JOIN against the metric view, exposed as a flat consumer view. Don't propose plain `LAG(...) OVER (...)` against the source as the migration target — it loses the governance the metric view provides.

8. **Spot-check totals.** Once data is loaded, run `MEASURE(\`Total Revenue\`)` queries against the metric view and compare with the same measure in Power BI on a sample slice. Bidirectional cross-filtering and time intelligence are the usual divergence points.

## Flags

| Flag | What it controls |
|------|------------------|
| `--style kimball` (default) | Tables: `dim_*`, `fact_*`. Columns: snake_case lowercase. No Delta column mapping. |
| `--style fidelity` | Tables: original PBI names. Columns: original (with spaces). DDL emits Delta `columnMapping.mode='name'` automatically. |
| `--catalog-schema CATALOG.SCHEMA` | Three-level prefix for table refs in joins and CREATE statements. Required for runnable SQL. |
| `--source CATALOG.SCHEMA.FACT` | Explicit override for the metric view's `source:`. Wins over `--catalog-schema` (also accepts a SQL block — useful for adding LAG cols by hand). |
| `--fact-table NAME` | Pick a specific PBI table as the fact (default: most measures). |
| `--fact-suffix WORD` | Domain suffix for kimball fact-table name. `--fact-suffix profitability` → `fact_profitability`. |
| `--emit-ddl` | Emit CREATE TABLE DDL alongside the metric view (default off — emits only the view). |
| **`--verify`** | Run static schema check + structural diff against the source PBIT. Reports unresolved column refs, alias mismatches, count drift to stderr. |
| **`--emit-verify-sql`** | Append a verification SQL block — one `SELECT MEASURE(\`name\`) FROM view LIMIT 1` per measure. Run this against Databricks to compile-check every measure expression. |
| `--strict` | Exit non-zero if `--verify` found issues OR any measure was flagged for manual review. CI guard. |
| **`--no-dim-metadata`** | Strip `comment:` and `synonyms:` from every dimension. Use this if your workspace runs a metric-view serde older than v1.1 (DBR < 17.2) — those parsers know only `name/expr/window` on dims and reject the YAML with `Unrecognized field 'synonyms' (class … v10.Column), not marked as ignorable (3 known properties: 'window', 'name', 'expr')`. Measures keep full metadata (their schema is stable across older serdes). DBR 17.2+ should leave this off. |

## Verification levels

The skill catches different bug classes at different points:

| Level | Catches | When | How |
|---|---|---|---|
| 1. Translation unit tests | DAX→SQL pattern bugs | Before commit | `python3 scripts/test_translation.py` |
| 2. Diagnostic warnings | Measures the translator punted on (DATEADD, TOTALYTD, etc.) | Every run | stderr output — flagged measures listed |
| 3. **Static schema check** | Column ref typos, alias mismatches, MEASURE() refs to missing names, unresolved bare brackets | `--verify`, no DB needed | walks every dim/measure expr, resolves backticks against source/joined columns |
| 5. **Live compile check** | Syntax errors, type mismatches, anything the static check can't see (window measure validity, join evaluation) | `--emit-verify-sql` then run via `execute_sql` | `SELECT MEASURE(\`X\`) FROM view LIMIT 1` per measure — fails fast on any expression that doesn't compile |
| 6. **Structural diff** | Lost or extra tables / measures / joins between PBIT and YAML | `--verify`, no DB needed | counts source PBIT vs generated metric view |
| 7. Numerical equivalence | DAX vs SQL produce same numbers | Manual | sample queries in PBI Desktop vs Databricks, compare CSVs |

## Synonyms (auto) and comments (LLM-authored by the agent)

The converter emits two distinct fields per **measure AND dimension** for documentation and discovery. Both objects get the same treatment — Genie/AI/BI search hits dimensions and measures equally, so dropping metadata on either side leaves a discovery hole.

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

Every emitted dimension carries a `synonyms:` list. PBI columns are referenced in DAX as `'Table'[Column]` (qualified) — that's what the converter emits, alongside the bare column name. Same dedup rule: anything matching `name:` is dropped.

- **Joined-table dim** (`name:` = `<Table> <Column>`): both forms differ from `name:`, so both are kept.
  ```yaml
  - name: D_Calendar Date
    expr: d_calendar.`date`
    synonyms:
      - Date                        # bare PBI col name
      - "'D_Calendar'[Date]"        # canonical DAX qualified form
  ```

- **Fact-table dim where `name:` already matches the column**: bare-name form dedups, only the qualified DAX form remains.
  ```yaml
  - name: fy
    expr: '`fy`'
    synonyms:
      - "'fact_corp_kpi_ww_daily_rev'[fy]"
  ```

- If both forms equal `name:` (rare), the `synonyms:` block is omitted entirely.

Databricks metric views accept `synonyms:` per **measure and dimension** as a list. Genie / AI/BI consumers use it to resolve user queries to the right object when the migrated `name:` differs from what was typed. The DAX bracket / qualified form is critical: anyone reading the original PBI report or DAX code will type that, and synonyms make the migrated object findable. The converter populates this **mechanically** for every measure and dimension — pure 1:1 mapping from the source PBI metadata, with the dedup rule above.

**DBR version note:** Measure `synonyms:`/`comment:` work on every recent DBR. **Dimension** `synonyms:`/`comment:` require the **v1.1 metric-view serde (DBR 17.2+)**; older runtimes (DBR 16.4–17.1, internal class `v10.Column`) only know `name/expr/window` on dims and reject the YAML with `Unrecognized field 'synonyms' … 3 known properties: 'window', 'name', 'expr'`. If you hit that error, either upgrade DBR or re-run the converter with `--no-dim-metadata` to strip dim `comment:`/`synonyms:` (measures unaffected).

### `comment:` — LLM-authored by the agent (NOT rule-based)

Comments are **the agent's responsibility**, not the converter's, for both measures and dimensions. The converter:

- Preserves the source PBI `description` field verbatim if present (human-authored documentation always wins) — this applies to both measure descriptions and column descriptions.
- Emits NO auto-comment otherwise — leaves a `# comment: TODO — agent should LLM-author …` placeholder in the YAML so the gap is visible.

Why no rule-based auto-comment: regex pattern detection over DAX or column names produces shallow, generic, and often misleading text ("Ratio of x to expr", "Sum of qtd_shpmt at yesterday's snapshot", "The bg_cd column") that's worse than no comment. The DAX shape / column name doesn't capture business meaning — what users actually need to know — and once a misleading comment exists in UC Catalog, downstream tools (Genie, LLM agents indexing the catalog) will treat it as ground truth.

**Agent workflow when applying this skill:**

1. Run the converter to produce the YAML with `synonyms:` populated and `comment:` either preserved-from-PBI or left as a TODO placeholder. **Both `dimensions:` and `measures:` get TODO placeholders for any object lacking a PBI description.**
2. For each measure AND dimension, **the agent reads** the relevant context: for measures, the original DAX expression and join context; for dimensions, the column physical type, related joined-table semantics, and how the column is used in measure filters (e.g. a `column <> 1` filter on `column` reveals it's a dedup flag).
3. **The agent writes** a one-line `comment:` capturing (a) what the object means in business terms, (b) units / scale where applicable (revenue in millions, count of customers, ratio 0–1, BG×Geo×Date composite key, etc.), and (c) any non-obvious filter, scope, or convention (latest snapshot, excluding ISGPRC1, ISO-format date, fiscal-year string like "FY24", etc.).
4. The agent emits the final `CREATE OR REPLACE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$` with both `synonyms:` (auto) and `comment:` (LLM-authored) populated for **every dimension and every measure**.

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

## Quick reference — what auto-translates

Patterns the converter handles end-to-end (no human edit needed):

| DAX | Metric view SQL |
|-----|-----------------|
| `SUM/AVERAGE/MIN/MAX/COUNT('T'[c])` | `SUM/AVG/MIN/MAX/COUNT(\`c\`)` |
| `COUNTA('T'[c])` | `COUNT(\`c\`)` |
| `COUNTROWS('T')` | `COUNT(1)` |
| `DISTINCTCOUNT('T'[c])` | `COUNT(DISTINCT \`c\`)` |
| `CALCULATE(agg, 'T'[col]=val)` | `agg FILTER (WHERE \`col\` = val)` |
| `CALCULATE(agg, FILTER('T', expr))` | `agg FILTER (WHERE expr)` (FILTER table-arg unwrapped) |
| `CALCULATE(agg, LASTDATE('Cal'[Date]))` | `agg FILTER (WHERE is_latest_snapshot)` — **and** auto-augments the metric view's `source:` with `is_latest_snapshot = (date = MAX(date) OVER())` |
| `CALCULATE(agg, FIRSTDATE('Cal'[Date]))` | `agg FILTER (WHERE is_first_snapshot)` (same shape) |
| `CALCULATE(agg, DATEADD(LASTDATE('Cal'[Date]), -1, DAY))` | `agg FILTER (WHERE is_yesterday_snapshot)` — auto-augments source with `DENSE_RANK() OVER (ORDER BY date DESC) = 2`. Works for snapshot tables with daily writes. |
| `CALCULATE(SUM(x)/10^6, p)` | `SUM(x) FILTER (WHERE p) / 1000000` — FILTER attaches to the aggregate, NOT after the / division (skill catches this; manual edits should follow the same rule). |
| `DIVIDE(a, b)` | `(a) / NULLIF((b), 0)` |
| `IFERROR(a, b)` | `a` (NULLIF in DIVIDE handles /0 — the typical IFERROR use) |
| `IF(c, a, b)` / `IF(c, a)` | `CASE WHEN c THEN a ELSE b END` / `CASE WHEN c THEN a END` |
| `IF(SELECTEDVALUE(...)='X', "-", real)` | unwrapped to `real` (UI edge-case display dropped). Recursively peels nested IF chains; runs both before AND after VAR/RETURN inlining so `VAR Output = IF(slicer, "-", core) RETURN Output` also unwraps to `core`. |
| `IF(NOT SELECTEDVALUE(...) IN {"A","B"} && [foo]<>BLANK(), "-", real)` | unwrapped to `real`. Compound `&&` conditions and `NOT … IN {…}` set membership all handled. |
| `IF(<expr>=BLANK(), "-", <expr>)` / `IF(<expr><>BLANK(), <expr>, "-")` | unwrapped to `<expr>`. Pure UI fallback — drops the placeholder branch. After translation, `=BLANK()` becomes `IS NULL` and is also recognized. |
| `SWITCH(x, v1, r1, ..., default)` | `CASE WHEN x = v1 THEN r1 ... ELSE default END` |
| `SWITCH(TRUE(), c1, r1, ..., default)` | `CASE WHEN c1 THEN r1 ... ELSE default END` (boolean-conditioned) |
| `BLANK()` | `NULL` |
| `TRUE()` / `FALSE()` | `TRUE` / `FALSE` |
| `&&` / `\|\|` | `AND` / `OR` |
| `&` (string concat) | `\|\|` |
| `^` (`10^6`) | `1000000` (literal expansion) |
| `FORMAT(x, "fmt")` | `x` (format strings are consumer-side) |
| `CONCATENATE(a, b)` | `concat(a, b)` |
| `'T'[col]` | `` `col` `` |
| `[Measure Name]` | `MEASURE(\`Measure Name\`)` if the bracketed name matches a declared measure; else `` `Measure Name` `` (column on fact). |
| `VAR x = e1 VAR y = e2 ... RETURN body` | bindings inlined into body — works for arbitrary nesting (recursive substitution). |

For mechanism details (CALCULATE→FILTER unwrap, LASTDATE source-SQL augmentation, VAR/RETURN inlining, forward-reference topo-sort), see `references/dax-to-sql-patterns.md`.

## Flagged for manual review (output preserves original DAX)

| Construct | Why | Suggested rewrite |
|-----------|-----|-------------------|
| `DATEADD(..., -N, MONTH/YEAR)`, `TOTALYTD`, `SAMEPERIODLASTYEAR`, `PARALLELPERIOD`, `PREVIOUS{MONTH,QUARTER,YEAR}` | Time-shift requires a window measure or a SQL source with `LAG()`. | Add a `window:` block with `range: trailing N month/year` (or `cumulative` for YTD) ordered on a date dim. **The `expr:` MUST be `MEASURE(\`Base Measure\`)`, never a re-inlined `SUM(...)`** — see non-negotiable default #4. Alternative: augment `source:` with `LAG(...)` columns and reference them. See `references/dax-to-sql-patterns.md` § Period-over-period patterns. (Note: `DATEADD(LASTDATE, -1, DAY)` is auto-translated to `is_yesterday_snapshot`.) |
| `ALL('geo')`, `ALLEXCEPT`, `ALLSELECTED` used to compute a "WW total" override | Drops filter context — no SQL analog. | Bake each unfiltered total into a separate measure (e.g. `Revenue WW`, `Revenue ISO`); the user picks the one they want at query time. Don't try to encode the "if slicer=X show WW else show local" branching — it's a UI display rule. |
| `USERELATIONSHIP` | Activates an inactive relationship. | Add a second `joins:` entry; pick which to filter at query time. |
| `EARLIER`, `EARLIEST`, `RANKX`, `TOPN` | Row-context evaluation. | Rewrite as a window function in the SQL `source:` or a wrapper view. |
| `LOOKUPVALUE('Dim'[col], 'Dim'[key], <key_expr>)` | Star-schema lookup with row-context. | Reference the joined table directly: `dim.col`. The skill auto-flags this; the rewrite is mechanical when the join already exists. |
| `RELATED('Dim'[col])` | Star-schema lookup. | Reference the joined table directly: `dim.col`. The converter assumes you'll wire joins via the `joins:` block. |
| Outer `SELECTEDVALUE/ISFILTERED` not in an `IF(..., "-", real)` wrapper (e.g. dispatching like `IF(SELECTEDVALUE(BG)="PCSD", PCSD_Units, StandardUnits)`) | Generic slicer-context manipulation. | Drop the slicer-conditional branch and ship the simpler "real" calculation. Or split into 2 measures (`Units PCSD` and `Units Standard`); user picks at query time. Last resort: expose the slicer column as a query-time WHERE on a dimension. |
| `SUM(...)+SUM(...)` constructed across two separate fact tables | Cross-fact arithmetic; metric view has one `source` | Build a separate metric view per fact source. Or, if joinable on PK, expose one as a `joins:` entry and reference its column. |

## What NOT to migrate (skip these)

These are intentionally UI-only or non-aggregable — they aren't real KPIs and shouldn't be carried into the metric view:

| Pattern | What it is in PBI | Why skip |
|---------|-------------------|----------|
| `Test_*`, `*_Format`, parameter-table SWITCH-on-index measures | DAX measures that route to a "selected" measure based on `SELECTEDVALUE('Param'[Index])` from a hidden parameter table — drives buttons/toggles in the Power BI visual. | Pure visual dispatch; no SQL analog. The underlying measures are already migrated; users select them by name. |
| Color/format string measures (`'#77B947'`, `'#DE6A73'`) | Conditional formatting expressions returning hex color codes. | Not numeric. Conditional formatting belongs on the consumer (AI/BI dashboard, Power BI). |
| `"Cutoff Date up to " & LASTDATE(...)` and similar string-display measures | Title/header label measures. | Not aggregable. Use a SQL view or compute at query time. |
| `*_Modified` family with `SWITCH(TRUE(), max('Period Selection'[Index])=2, CALCULATE(<base>, Last_20days = "Last 20 Days"), …)` | Period-Selection slicer dispatch — dispatches the base measure with a `Last_20days`/`Last_3days` filter. | Replace with: expose `Last_20days`, `Last_3days` (whatever filter columns the SWITCH dispatches on) as **dimensions** on the metric view. The user replicates the modified behavior with a query-time `WHERE Last_20days = 'Last 20 Days'`. Don't try to encode the SWITCH. |

## Quick reference — common follow-up edits

| Symptom | Fix |
|---------|-----|
| `<TODO_CATALOG.SCHEMA>.X` still in YAML | Replace with the real three-level UC name. |
| Wrong fact table picked (too few measures show up) | Re-run with `--fact-table TableName`. |
| Dim columns leaked as dimensions but you want fewer | Delete those entries from `dimensions:`. |
| Measure has `# TODO` for time intel | Add a `window:` block, or augment `source:` with LAG cols. See `references/dax-to-sql-patterns.md` § Period-over-period patterns. |
| Measure has `# TODO` for `RELATED('Dim'[col])` | Replace `RELATED(...)` with `joinname.col`. |
| `BINARY_OP_DIFF_TYPES` on `INTERVAL '-1' YEAR` | The window's `order` dim is INT — change it to `DATE_TRUNC('YEAR', date)`. |
| `MEASURE(\`X\`) FILTER (WHERE ...)` rejected by engine | `MEASURE()` doesn't support FILTER clause. Inline the underlying agg expression instead. |
| Aggregating a STRING column (e.g. revenue stored as text) | The DDL emits the original type. Either fix the DDL/cast on ingest, or wrap the column in `try_cast(... AS DOUBLE)` inside the SQL `source:`. |
| `[METRIC_VIEW_INVALID_VIEW_DEFINITION] Unrecognized field "synonyms" (class … v10.Column), not marked as ignorable (3 known properties: "window", "name", "expr")` | Your DBR is on a metric-view serde older than v1.1 (DBR < 17.2). That parser only knows `name/expr/window` on dimensions. Re-run the converter with `--no-dim-metadata` to strip dim `comment:`/`synonyms:`. (Measures are unaffected.) Or upgrade DBR. |

## Files

- `scripts/dax_to_metric_view.py` — converter (Python stdlib only; reads `.pbit`).
- `scripts/test_translation.py` — unit tests for the DAX→SQL translator (no `.pbit` needed).
- `references/dax-to-sql-patterns.md` — translation cheatsheet, period-over-period patterns, JOIN-CTEs migration shape.
- `references/extracting-pbi-models.md` — getting various PBI sources to a `.pbit`.

## Common mistakes

- **Trying to feed a `.pbix` directly.** The converter rejects `.pbix`. Export to `.pbit` from PBI Desktop first — that takes 1 click and gives you the full schema as JSON. Working around with pbixray (the prior approach) silently drops base columns from M-loaded tables.
- **PBI auto date/time hidden tables.** When **Auto date/time** is enabled in Power BI Desktop (the default), PBI silently generates one `DateTableTemplate_<GUID>` plus one `LocalDateTable_<GUID>` per Date column to drive the auto Year→Quarter→Month→Day hierarchy. They contain no real business data. The converter excludes them from DDL emission, joins, dimensions, and fact-table picking via the `_AUTO_DATE_PREFIXES` filter.
- **Auto-loading source data.** Don't. "Convert to tables" means structure. Data ingestion is a separate explicit step.
- **Defaulting to fidelity style without being asked.** Genie / AI / dbt expect Kimball snake_case.
- **Trusting bidirectional cross-filter behavior.** Power BI's bidirectional filters silently change measure totals. Metric view joins are one-directional SQL `ON`. Sample-check totals against PBI before publishing.
- **Forgetting `--catalog-schema`.** Without it, the YAML has `<TODO_CATALOG.SCHEMA>.<FactName>` placeholders and the SQL won't run.
- **Ignoring the PBI author's commented-out "official" measure.** When a measure starts with `// =SUM(…) / 10^6 this is the official measure` or `// SUM(…) this is the official` followed by a workaround using `DATEADD`/`LASTDATE`/`CALCULATE`, prefer the *commented* official definition. The workaround is a band-aid for an upstream data lag the PBI author is waiting to fix; the simple commented version is what they intend the measure to be once the data is right.
- **Putting `FILTER` after the `/ N` division.** SQL `FILTER (WHERE …)` is a clause on the aggregate, not on the surrounding arithmetic. `SUM(x) FILTER (WHERE p) / 1000000` ✓ — `SUM(x) / 1000000 FILTER (WHERE p)` ✗ (parse error). The converter handles this; manual edits should follow the same rule.
- **Wrapping `MEASURE()` in `FILTER (WHERE …)`.** The engine rejects this. Inline the underlying aggregate expression (e.g. `SUM(qtd_target_mx_track) FILTER (WHERE is_latest_snapshot) / 1000000` instead of `MEASURE(\`Load Track\`) FILTER (WHERE is_latest_snapshot)`).
- **Treating inactive relationships as auto-translated.** The converter skips relationships where `isActive=False` and emits a warning. If a measure depends on `USERELATIONSHIP`, wire the join manually.
- **Inlining raw `SUM(...)` in windowed measures instead of `MEASURE()` refs.** Already covered as non-negotiable default #3 — *do not* re-inline. If you find yourself typing `SUM(\`material_costs\`)+SUM(\`labor_costs_variable\`)+...` in a `window:` block, stop and replace with `MEASURE(\`Total COGS\`)`. The window engine resolves the referenced measure's aggregate first, then applies the window — this is what makes `Gross Margin SPLY = MEASURE(\`Gross Margin\`)` with `window: trailing 1 year` work cleanly. The author saw this mistake in the wild on a real PBI migration; the fix saved 7-component edits per measure-definition change.
- **Forgetting that `SUM('T'[col])` on a STRING column won't work.** If the source column is text but the DAX treats it numerically, either fix the source column type (DDL/ingest) or `try_cast` it inside the metric view's `source:` SQL.
