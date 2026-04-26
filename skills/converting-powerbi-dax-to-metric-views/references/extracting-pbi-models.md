# Getting a `.pbit` for the converter

The converter takes **only `.pbit`** (Power BI Template). Everything below is about getting one.

## You already have a `.pbix`

```text
PBI Desktop → File → Export → Power BI Template (.pbit)
```

That's it. The `.pbit` is dropped next to your `.pbix`. PBI Desktop strips data and serializes the model as `DataModelSchema` JSON inside the ZIP — exactly what the converter reads.

## You already have a `.pbit`

```bash
python3 scripts/dax_to_metric_view.py model.pbit \
  --catalog-schema main.sales --emit-ddl --out main_sales.sql
```

Done.

## You have a `.bim` or TMSL JSON (from Tabular Editor / SSDT)

1. Open the `.bim` in Tabular Editor 2 (or 3).
2. Deploy it to a local Analysis Services instance OR import it into a new Power BI Desktop file.
3. From Power BI Desktop, **File → Export → Power BI Template (`.pbit`)**.

There is no direct `.bim → .pbit` converter; the round-trip goes through Power BI Desktop.

## You have a workspace dataset (no local file)

1. In Power BI Desktop, **Get Data → Power BI semantic models** and connect to the workspace dataset.
2. Build the local model on top, then **File → Export → `.pbit`**.

For Premium / PPU workspaces with the XMLA endpoint, you can also use Tabular Editor 2 + a "save as Power BI Template" command — but the simpler path above usually works.

## Why `.pbit` and not `.pbix`

A `.pbix` stores the model in a single binary partition (`DataModel`) that is XPRESS9-compressed Analysis Services tabular backup. The format is undocumented; community readers (pbixray, etc.) reverse-engineer it incompletely. In particular, **base columns from M-loaded tables are routinely missed** — for example, a typical `D_Calendar` with a `Date` PK and several Year/Quarter/Day source columns will only expose its DAX-calculated columns, leaving the converter blind to half the schema.

A `.pbit` has the **same metadata as plain JSON** in `DataModelSchema`:
- All tables (calculated, M-loaded, hybrid)
- All columns (source + calculated) with types, hidden flags, summarizeBy
- All measures with full DAX expressions
- All relationships, including isActive and crossFilteringBehavior

The converter parses this JSON with `zipfile + json` from the Python stdlib — no third-party deps.

## What the converter reads from the `.pbit`

The DataModelSchema JSON follows the [TMSL spec](https://learn.microsoft.com/en-us/analysis-services/tmsl/tabular-model-scripting-language-tmsl-reference). The converter uses:

| Field | TMSL path |
|-------|-----------|
| Tables | `model.tables[]` (excluding `LocalDateTable_*` / `DateTableTemplate_*` auto-date hidden tables) |
| Columns + types | `model.tables[].columns[]` — `name`, `dataType`, `isHidden`, `summarizeBy`, `expression` (for calc cols) |
| Measures (DAX) | `model.tables[].measures[]` — `name`, `expression` (string or array of lines), `description` |
| Relationships | `model.relationships[]` — `fromTable`, `fromColumn`, `toTable`, `toColumn`, `isActive` |

It ignores (by design): M / Power Query expressions (the M source code), partitions other than calculated-table detection, perspectives, RLS roles, KPIs, calculation groups, format strings, data source connections, annotations, hierarchies.

## Edge cases

- **Hidden columns** (`isHidden=true`) are excluded from the metric view's `dimensions:` list but still emitted in DDL — that way joins still work and consumers can selectively expose more.
- **Calculated tables** (built via `= CALENDAR(...)` etc.) come through with their materialized columns. The converter detects these via `partitions[].source.type == "calculated"` and emits DDL the same way.
- **Calculated columns** (with a `expression` field) come through with their type metadata; their DAX is preserved as a comment in DDL — but they are not auto-translated, since the metric view computes calc-col-equivalent values via measures or dimension expressions.
- **Multi-line measure expressions** are stored in TMSL as a JSON array; the converter joins them with newlines before translating.

## Limitations

- DAX measure expressions come through verbatim — the converter normalizes whitespace and strips DAX comments before translating, but does not "interpret" arbitrary functions. If a measure uses something exotic (e.g., a calculation group selector or an obscure DAX function not in the auto-translation table), the converter passes it through and flags it for manual review with the original DAX preserved as a YAML comment.
- The translator does NOT know which DAX measures are flagged hidden — all measures are emitted. Trim the YAML manually if you only want a subset exposed.
