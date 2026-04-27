#!/usr/bin/env python3
"""Lightweight tests for the PBI→metric-view scaffolder helpers.

Run: python3 test_translation.py

The skill is a SCAFFOLDER, not a translator — DAX→SQL translation is the
agent's (LLM) job, not the script's. So these tests cover only the
mechanical helpers the script provides:

  - build_measure_synonyms / build_dimension_synonyms (1:1 PBI mapping)
  - kimball_col / kimball_table (snake_case rename rules)
  - _is_real_table / _AUTO_DATE_PREFIXES (PBI auto-date filter)
  - _yaml_scalar (YAML quoting)

These are stable, testable rules. Translation correctness is verified end-to-end
via the live compile-check SQL block (--emit-verify-sql), not unit tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dax_to_metric_view import (  # noqa: E402
    build_measure_synonyms,
    build_dimension_synonyms,
    kimball_col,
    kimball_table,
    _is_real_table,
    _yaml_scalar,
    emit_table_ddl,
)


def test_measure_synonyms() -> tuple[int, int]:
    """build_measure_synonyms: emits the bracketed DAX form. Bare original name
    is included only when it differs from the metric-view name (i.e. when a
    hand-edit renamed the measure to drop unsafe chars)."""
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
        print(f"{mark} msyn  {label:40s}  → {got}")
        if not ok:
            print(f"    expected: {expected}")
            failures += 1
    return len(cases), failures


def test_dimension_synonyms() -> tuple[int, int]:
    """build_dimension_synonyms: emits bare PBI col + 'Table'[Column] DAX form,
    skipping any entry that equals the metric-view dim_name."""
    failures = 0
    cases = [
        ("join dim — both forms differ from name",
         "D_Calendar", "Date", "D_Calendar Date",
         ["Date", "'D_Calendar'[Date]"]),
        ("fact dim, name=col — only DAX form",
         "fact_corp_kpi_ww_daily_rev", "fy", "fy",
         ["'fact_corp_kpi_ww_daily_rev'[fy]"]),
        ("kimball-renamed col — both forms",
         "Sales", "Total Sales (USD)", "total_sales_usd",
         ["Total Sales (USD)", "'Sales'[Total Sales (USD)]"]),
        ("dim_name equals DAX form — only bare",
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


def test_kimball_naming() -> tuple[int, int]:
    """kimball_col / kimball_table: snake_case rename rules."""
    failures = 0
    col_cases = [
        ("BG+GEO+Date", "bg_geo_date"),
        ("New BG", "new_bg"),
        ("D_Calendar Date", "d_calendar_date"),
        ("Last_20days", "last_20days"),
        ("FY", "fy"),
        ("Quarter Day1", "quarter_day1"),
        ("Customer Group (ISG)", "customer_group_isg"),
    ]
    for inp, want in col_cases:
        got = kimball_col(inp)
        ok = got == want
        mark = "✓" if ok else "✗"
        print(f"{mark} kcol  {inp!r:40s}  → {got!r}")
        if not ok:
            print(f"    expected: {want!r}")
            failures += 1

    tbl_cases = [
        # (name, is_fact, fact_suffix, expected)
        ("D_Calendar", False, None, "dim_d_calendar"),
        ("dim_geo", False, None, "dim_geo"),  # already prefixed → no double prefix
        ("Sales", True, "profitability", "fact_profitability"),
        ("fact_orders", True, None, "fact_orders"),  # already prefixed
        ("Daily Revenue", True, "revenue", "fact_revenue"),
    ]
    for name, is_fact, suffix, want in tbl_cases:
        got = kimball_table(name, is_fact, suffix)
        ok = got == want
        mark = "✓" if ok else "✗"
        print(f"{mark} ktbl  {name!r:40s}  → {got!r}")
        if not ok:
            print(f"    expected: {want!r}")
            failures += 1

    return len(col_cases) + len(tbl_cases), failures


def test_auto_date_filter() -> tuple[int, int]:
    """_is_real_table: filters PBI auto date/time hidden tables."""
    failures = 0
    cases = [
        ("LocalDateTable_abc123", False),
        ("DateTableTemplate_xyz", False),
        ("D_Calendar", True),
        ("FactSales", True),
        ("dim_geo", True),
    ]
    for name, want in cases:
        got = _is_real_table(name)
        ok = got == want
        mark = "✓" if ok else "✗"
        print(f"{mark} adf   {name!r:40s}  → {got}")
        if not ok:
            print(f"    expected: {want}")
            failures += 1
    return len(cases), failures


def test_yaml_scalar_quoting() -> tuple[int, int]:
    """_yaml_scalar: quotes only when needed (YAML special chars / leading
    space-or-dash). Plain identifiers stay raw to keep the YAML readable."""
    failures = 0
    cases = [
        # (input, must_be_quoted)
        ("Total Revenue", False),  # plain string
        ("BG+GEO+Date", False),  # `+` is not in the special-chars set
        ("`fy`", True),  # backtick → quoted
        ("foo: bar", True),  # colon → quoted
        ("a, b", True),  # comma → quoted
        ("- leading dash", True),  # leading `-` → quoted
        (" leading space", True),  # leading space → quoted
        ("", True),  # empty → "" sentinel
    ]
    for inp, must_quote in cases:
        got = _yaml_scalar(inp)
        is_quoted = got.startswith('"') and got.endswith('"')
        ok = is_quoted == must_quote
        mark = "✓" if ok else "✗"
        print(f"{mark} yaml  {inp!r:40s}  → {got!r} (quoted={is_quoted})")
        if not ok:
            print(f"    expected quoted={must_quote}")
            failures += 1
    return len(cases), failures


def test_ddl_excludes_auto_date_tables() -> tuple[int, int]:
    """emit_table_ddl: skips PBI auto-date hidden tables."""
    failures = 0
    doc = {
        "tables": [
            {"name": "FactSales", "columns": [{"name": "amount", "dataType": "double"}]},
            {"name": "LocalDateTable_abc", "columns": [{"name": "Date", "dataType": "datetime"}]},
            {"name": "DateTableTemplate_xyz", "columns": [{"name": "Year", "dataType": "int64"}]},
        ],
        "relationships": [],
    }
    ddl = emit_table_ddl(doc, "main.sales", "kimball", "FactSales", "sales")
    ok = ("fact_sales" in ddl
          and "LocalDateTable" not in ddl
          and "DateTableTemplate" not in ddl)
    mark = "✓" if ok else "✗"
    print(f"{mark} ddl   excludes PBI auto-date tables")
    if not ok:
        failures += 1
        print(f"    DDL:\n{ddl}")
    return 1, failures


def main() -> int:
    failures = 0
    total = 0

    for fn in (
        test_measure_synonyms,
        test_dimension_synonyms,
        test_kimball_naming,
        test_auto_date_filter,
        test_yaml_scalar_quoting,
        test_ddl_excludes_auto_date_tables,
    ):
        n, f = fn()
        total += n
        failures += f

    print(f"\n{total - failures} passed, {failures} failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
