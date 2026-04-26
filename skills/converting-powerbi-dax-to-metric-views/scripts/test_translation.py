#!/usr/bin/env python3
"""Lightweight tests for the DAX→SQL translator. Run: python3 test_translation.py.

These tests do not need a real .pbit — they exercise translate_dax_expr() directly
and a few of the helpers (build_metric_view, emit_table_ddl).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dax_to_metric_view import (  # noqa: E402
    translate_dax_expr,
    build_metric_view,
    emit_table_ddl,
    inline_var_return,
    unwrap_slicer_if,
    expand_power_literals,
    strip_dax_comments,
    topo_sort_measures,
    build_measure_synonyms,
    build_dimension_synonyms,
)


# (label, dax, expected_substring_in_sql)
CASES_AUTO_TRANSLATED = [
    ("SUM",          "SUM('Sales'[Amount])",                                  "SUM(`Amount`)"),
    ("COUNTROWS",    "COUNTROWS('Sales')",                                     "COUNT(1)"),
    ("DISTINCTCNT",  "DISTINCTCOUNT('Sales'[CustomerId])",                     "COUNT(DISTINCT `CustomerId`)"),
    ("AVERAGE",      "AVERAGE('Sales'[Amount])",                               "AVG(`Amount`)"),
    ("MIN",          "MIN('Sales'[Amount])",                                   "MIN(`Amount`)"),
    ("MAX",          "MAX('Sales'[Amount])",                                   "MAX(`Amount`)"),
    ("CALCULATE+filter",
     'CALCULATE(SUM(\'Sales\'[Amount]), \'Sales\'[Status] = "O")',
     "SUM(`Amount`) FILTER (WHERE `Status` = 'O')"),
    ("CALCULATE+FILTER(table)",
     'CALCULATE(SUM(\'Sales\'[Amount]), FILTER(\'Sales\', \'Sales\'[Date] >= "2024-01-01"))',
     "FILTER (WHERE `Date` >= '2024-01-01')"),
    ("CALCULATE+LASTDATE",
     "CALCULATE(SUM('Sales'[Amount]), LASTDATE('Cal'[Date]))",
     "FILTER (WHERE is_latest_snapshot)"),
    ("CALCULATE+FIRSTDATE",
     "CALCULATE(SUM('Sales'[Amount]), FIRSTDATE('Cal'[Date]))",
     "FILTER (WHERE is_first_snapshot)"),
    ("DIVIDE",
     "DIVIDE(SUM('Sales'[Amount]), DISTINCTCOUNT('Sales'[CustomerId]))",
     "/ NULLIF((COUNT(DISTINCT `CustomerId`)), 0)"),
    ("IF/2-arg", 'IF(\'Sales\'[Amount] > 1000, "high")',
     "CASE WHEN `Amount` > 1000 THEN 'high' END"),
    ("IF/3-arg", 'IF(\'Sales\'[Amount] > 1000, "high", "low")',
     "CASE WHEN `Amount` > 1000 THEN 'high' ELSE 'low' END"),
    ("IFERROR unwrap", 'IFERROR(DIVIDE([Rev], [Qty]), BLANK())', "[Rev]"),
    ("BLANK→NULL", "BLANK()", "NULL"),
    ("FORMAT unwrap", 'FORMAT(SUM(\'S\'[A]), "#,##0.00")', "SUM(`A`)"),
    ("CONCATENATE", 'CONCATENATE("a","b")', "concat('a', 'b')"),
    ("SWITCH",
     'SWITCH(\'Sales\'[Status], "O", "Open", "C", "Closed", "Other")',
     "CASE WHEN `Status` = 'O' THEN 'Open'"),
    ("SWITCH(TRUE())",
     'SWITCH(TRUE(), \'A\'[x] = 1, "one", \'A\'[x] = 2, "two", "other")',
     "CASE WHEN `x` = 1 THEN 'one'"),
    ("AND/OR",
     "'Sales'[A] = 1 && 'Sales'[B] = 2 || 'Sales'[C] = 3",
     "`A` = 1  AND  `B` = 2  OR  `C` = 3"),
    ("string concat",
     '"Cutoff up to " & LASTDATE(\'Cal\'[Date])',
     "||"),
    ("10^6 literal",
     "SUM('S'[A]) / 10^6",
     "1000000"),
    ("VAR/RETURN inline",
     "VAR x = SUM('S'[A]) VAR y = SUM('S'[B]) RETURN x + y",
     "(SUM(`A`)) + (SUM(`B`))"),
    ("nested VAR substitution",
     "VAR a = SUM('S'[X]) VAR b = a * 2 RETURN b + 1",
     "((SUM(`X`)) * 2) + 1"),
    ("IF(SELECTEDVALUE) unwrap",
     'IF(SELECTEDVALUE(\'Geo\'[name]) = "PRC", "-", SUM(\'S\'[A]))',
     "SUM(`A`)"),
    # FILTER must attach to aggregate, not after the / division.
    ("FILTER attaches to agg, not arithmetic",
     "CALCULATE(SUM('S'[a]) / 10^6, 'S'[bg] = \"X\")",
     "SUM(`a`) FILTER (WHERE `bg` = 'X') / 1000000"),
    # SELECTEDVALUE-IF nested inside VAR Output gets unwrapped after VAR inlining.
    ("VAR Output IF(slicer) unwrap",
     'VAR core = SUM(\'S\'[a]) VAR Output = IF(SELECTEDVALUE(\'G\'[c])="PRC", "-", core) RETURN Output',
     "SUM(`a`)"),
    # IF(<expr>=BLANK(), "-", <expr>) → <expr>; same shape with VAR.
    ("IF(=BLANK(), placeholder, expr) unwrap",
     'IF(SUM(\'S\'[a])=BLANK(), "-", SUM(\'S\'[a]))',
     "SUM(`a`)"),
    # IF(<expr><>BLANK(), <expr>, "-") → <expr>
    ("IF(<>BLANK(), expr, placeholder) unwrap",
     'IF(SUM(\'S\'[a])<>BLANK(), SUM(\'S\'[a]), "-")',
     "SUM(`a`)"),
    # DATEADD(LASTDATE, -1, DAY) used in CALCULATE filter → is_yesterday_snapshot
    ("DATEADD(-1,DAY) → is_yesterday_snapshot",
     "CALCULATE(SUM('S'[a]), DATEADD(LASTDATE('Cal'[Date]), -1, DAY))",
     "FILTER (WHERE is_yesterday_snapshot)"),
    # Compound nesting: VAR + IF(slicer) at outer + IF(=BLANK()) at inner.
    ("compound VAR + slicer-IF + BLANK-IF",
     ('VAR core = SUM(\'S\'[a]) '
      'VAR O = IF(NOT SELECTEDVALUE(\'G\'[c]) IN {"A","B"} && [foo]<>BLANK(), "-", '
      '          IF(core=BLANK() && [foo]<>BLANK(), "-", core)) '
      'RETURN O'),
     "SUM(`a`)"),
]

# Patterns the translator should still flag for manual review.
CASES_FLAGGED = [
    ("TOTALYTD",            "TOTALYTD(SUM('Sales'[Amount]), 'Sales'[OrderDate])", "TOTALYTD"),
    ("SAMEPERIODLASTYEAR",  "CALCULATE(SUM('Sales'[Amount]), SAMEPERIODLASTYEAR('Date'[Date]))", "SAMEPERIODLASTYEAR"),
    ("DATEADD",             "CALCULATE(SUM('Sales'[Amount]), DATEADD('Date'[Date], -1, MONTH))", "DATEADD"),
    ("USERELATIONSHIP",     "CALCULATE(SUM('Sales'[Amount]), USERELATIONSHIP('Sales'[ShipDate], 'Date'[Date]))", "USERELATIONSHIP"),
    ("ALLSELECTED",         "CALCULATE(SUM('S'[A]), ALLSELECTED('Geo'))", "ALLSELECTED"),
    ("LOOKUPVALUE",         'LOOKUPVALUE(\'Dim\'[col], \'Dim\'[k], "x")', "LOOKUPVALUE"),
]


def _make_doc_with_auto_date_tables() -> dict:
    return {
        "tables": [
            {
                "name": "Sales",
                "columns": [
                    {"name": "Amount", "dataType": "decimal", "summarizeBy": "sum"},
                    {"name": "OrderDate", "dataType": "datetime", "summarizeBy": "none"},
                    {"name": "CustomerId", "dataType": "string"},
                ],
                "measures": [{"name": "Total Sales", "expression": "SUM('Sales'[Amount])"}],
            },
            {
                "name": "Customer",
                "columns": [
                    {"name": "CustomerId", "dataType": "string"},
                    {"name": "Name", "dataType": "string"},
                ],
            },
            {
                "name": "LocalDateTable_39c22ddb-27f3-4e6c-8a44-a3380850fcb4",
                "columns": [
                    {"name": "Date", "dataType": "datetime"},
                    {"name": "Year", "dataType": "int64"},
                ],
            },
            {
                "name": "DateTableTemplate_fe310476-3bb5-422b-85ff-9fd23f2cad67",
                "columns": [{"name": "Date", "dataType": "datetime"}],
            },
        ],
        "relationships": [
            {"fromTable": "Sales", "fromColumn": "CustomerId",
             "toTable": "Customer", "toColumn": "CustomerId", "isActive": True},
            {"fromTable": "Sales", "fromColumn": "OrderDate",
             "toTable": "LocalDateTable_39c22ddb-27f3-4e6c-8a44-a3380850fcb4",
             "toColumn": "Date", "isActive": True},
        ],
    }


def test_auto_date_tables_excluded() -> tuple[int, int]:
    """Confirm LocalDateTable_* / DateTableTemplate_* are never emitted."""
    doc = _make_doc_with_auto_date_tables()
    failures = 0

    mv, _ = build_metric_view(doc, source_override=None, fact_override=None,
                              style="kimball", catalog_schema="main.test",
                              fact_suffix="sales")
    join_sources = " ".join(j.get("source", "") for j in mv.get("joins", []))
    if "localdatetable" in join_sources.lower() or "datetabletemplate" in join_sources.lower():
        print(f"✗ filter  metric view joins leaked auto-date tables: {join_sources!r}")
        failures += 1
    else:
        print("✓ filter  metric view excludes auto-date tables from joins")

    ddl = emit_table_ddl(doc, catalog_schema="main.test", style="kimball",
                         fact_table_orig="Sales", fact_suffix="sales")
    if "localdatetable" in ddl.lower() or "datetabletemplate" in ddl.lower():
        print("✗ filter  DDL leaked auto-date tables")
        failures += 1
    else:
        print("✓ filter  DDL excludes auto-date tables")

    return 2, failures


def test_helpers() -> tuple[int, int]:
    """Direct tests of the inlining / unwrapping helpers."""
    failures = 0
    cases = [
        ("strip_dax_comments line",
         strip_dax_comments("SUM(x) // comment\n+ 1"),
         "SUM(x)"),
        ("strip_dax_comments block",
         strip_dax_comments("SUM(x) /* multi\nline */ + 1"),
         "SUM(x)"),
        ("expand_power_literals 10^6",
         expand_power_literals("/ 10^6"),
         "/ 1000000"),
        ("expand_power_literals 10^3",
         expand_power_literals("* 10^3"),
         "* 1000"),
        ("inline_var_return simple",
         inline_var_return("VAR x = 1 + 2 RETURN x * 3") or "",
         "(1 + 2) * 3"),
        ("inline_var_return chained",
         inline_var_return("VAR a = 1 VAR b = a + 2 RETURN b * 3") or "",
         "((1) + 2) * 3"),
        ("unwrap_slicer_if simple",
         unwrap_slicer_if('IF(SELECTEDVALUE(\'G\'[x]) = "Y", "-", SUM(A))'),
         "SUM(A)"),
        ("unwrap_slicer_if nested",
         unwrap_slicer_if('IF(SELECTEDVALUE(\'G\'[x]) = "Y", "-", IF(ISFILTERED(\'D\'[c]), "-", SUM(A)))'),
         "SUM(A)"),
    ]
    for label, got, want in cases:
        ok = want in got
        mark = "✓" if ok else "✗"
        print(f"{mark} helper {label:35s}  got: {got!r}")
        if not ok:
            print(f"    want substring: {want!r}")
            failures += 1
    return len(cases), failures


def test_measure_synonyms() -> tuple[int, int]:
    """build_measure_synonyms: always emits the bracketed DAX form. Bare
    original name is included only when it differs from the metric-view
    name (i.e. when a hand-edit renamed the measure)."""
    failures = 0
    cases = [
        # (label, orig_name, mv_name, expected_exact_list)
        ("no rename — bracket only",
         "Last Q AUR Actual", "Last Q AUR Actual",
         ["[Last Q AUR Actual]"]),
        ("renamed (% → Pct) — both forms",
         "QTD Order Load % ASP", "QTD Order Load Pct ASP",
         ["[QTD Order Load % ASP]", "QTD Order Load % ASP"]),
        ("renamed (special chars dropped)",
         "QTD Order Load_excl.ship", "QTD Order Load excl ship",
         ["[QTD Order Load_excl.ship]", "QTD Order Load_excl.ship"]),
        ("default arg (no rename)",
         "Total Revenue", None,
         ["[Total Revenue]"]),
    ]
    for label, orig, mv, expected in cases:
        got = build_measure_synonyms(orig, mv) if mv is not None else build_measure_synonyms(orig)
        ok = got == expected
        mark = "✓" if ok else "✗"
        print(f"{mark} syn   {label:40s}  → {got}")
        if not ok:
            print(f"    expected: {expected}")
            failures += 1
    return len(cases), failures


def test_dimension_synonyms() -> tuple[int, int]:
    """build_dimension_synonyms: emits bare PBI col + 'Table'[Column] DAX form,
    skipping any entry that equals the metric-view dim_name."""
    failures = 0
    cases = [
        # (label, orig_table, orig_col, dim_name, expected_exact_list)
        ("join dim — both forms differ from name",
         "D_Calendar", "Date", "D_Calendar Date",
         ["Date", "'D_Calendar'[Date]"]),
        ("fact dim, name=col — only DAX form",
         "fact_corp_kpi_ww_daily_rev", "fy", "fy",
         ["'fact_corp_kpi_ww_daily_rev'[fy]"]),
        ("kimball-renamed col — both forms",
         "Sales", "Total Sales (USD)", "total_sales_usd",
         ["Total Sales (USD)", "'Sales'[Total Sales (USD)]"]),
        ("dim_name equals DAX form too — empty list",
         "T", "X", "'T'[X]",
         ["X"]),
    ]
    for label, ot, oc, name, expected in cases:
        got = build_dimension_synonyms(ot, oc, name)
        ok = got == expected
        mark = "✓" if ok else "✗"
        print(f"{mark} dsyn  {label:40s}  → {got}")
        if not ok:
            print(f"    expected: {expected}")
            failures += 1
    return len(cases), failures


def test_topo_sort() -> tuple[int, int]:
    failures = 0
    # B refs A; topo sort should place A before B
    measures = [
        {"name": "B", "expr": "MEASURE(`A`) + 1"},
        {"name": "A", "expr": "SUM(`x`)"},
    ]
    sorted_m = topo_sort_measures(measures)
    names = [m["name"] for m in sorted_m]
    if names == ["A", "B"]:
        print("✓ topo   A before B")
    else:
        print(f"✗ topo   wanted ['A','B'], got {names}")
        failures += 1
    return 1, failures


def main() -> int:
    failures = 0
    total = 0

    for label, dax, expected in CASES_AUTO_TRANSLATED:
        sql, warnings, flags = translate_dax_expr(dax)
        ok = expected in sql
        mark = "✓" if ok else "✗"
        print(f"{mark} auto  {label:30s}  → {sql!r}")
        if not ok:
            print(f"    expected substring: {expected!r}")
            print(f"    warnings:           {warnings}")
            failures += 1
        total += 1

    for label, dax, expected_warn_substring in CASES_FLAGGED:
        sql, warnings, flags = translate_dax_expr(dax)
        joined = "; ".join(warnings)
        ok = any(expected_warn_substring in w for w in warnings)
        mark = "✓" if ok else "✗"
        print(f"{mark} flag  {label:30s}  warnings: {joined!r}")
        if not ok:
            print(f"    expected warning containing: {expected_warn_substring!r}")
            failures += 1
        total += 1

    auto_date_total, auto_date_failures = test_auto_date_tables_excluded()
    failures += auto_date_failures
    total += auto_date_total

    helper_total, helper_failures = test_helpers()
    failures += helper_failures
    total += helper_total

    topo_total, topo_failures = test_topo_sort()
    failures += topo_failures
    total += topo_total

    syn_total, syn_failures = test_measure_synonyms()
    failures += syn_failures
    total += syn_total

    dsyn_total, dsyn_failures = test_dimension_synonyms()
    failures += dsyn_failures
    total += dsyn_total

    print(f"\n{total - failures} passed, {failures} failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
