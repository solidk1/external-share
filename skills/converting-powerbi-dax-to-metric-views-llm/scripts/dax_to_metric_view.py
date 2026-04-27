#!/usr/bin/env python3
"""
PBI .pbit → Databricks UC metric view scaffold.

This is a SCAFFOLDER, not a translator. The DAX → metric-view-SQL translation
is the **agent's** (LLM) job, not this script's. The script:

  1. Parses .pbit (ZIP containing TMSL JSON in DataModelSchema) using only the
     stdlib. The .pbit format exposes the FULL tabular schema as JSON — every
     base column, calculated column, measure DAX, and relationship.
  2. Picks a fact table (most measures, or --fact-table override).
  3. Builds star-schema joins from active relationships (warns on inactive).
  4. Emits CREATE TABLE IF NOT EXISTS DDL for every table (kimball or
     fidelity style). Never destructive — existing tables are left intact.
     If the table already exists with a different schema, the agent is
     responsible for confirming with the user before dropping it.
  5. Emits a metric-view YAML SCAFFOLD where:
       - source / joins / dimensions are fully populated mechanically
       - synonyms are populated mechanically (PBI bracket / qualified form,
         deduped against name:)
       - each measure is a placeholder: original DAX preserved as a YAML
         comment, expr: AGENT_TRANSLATE_DAX, comment: AGENT_AUTHOR
  6. Optionally appends a verification SQL block — one
     `SELECT MEASURE(\`X\`) FROM view LIMIT 1` per measure (commented out
     until the agent fills in the expressions).

The agent applying this skill must:
  - Read each measure's preserved original DAX
  - Translate to metric-view SQL using its own LLM brain + the cookbook in
    `references/dax-to-sql-patterns.md`
  - LLM-author the comment from the DAX + business context
  - Replace the AGENT_TRANSLATE_DAX / AGENT_AUTHOR placeholders
  - Add snapshot-flag columns (is_latest_snapshot, is_yesterday_snapshot, ...)
    to source: when any measure needs them — see SKILL.md § snapshot flags

Two naming styles:
  --style kimball   (default) lowercase snake_case, dim_*/fact_* prefixes.
                    Genie / dbt / Fabric idiomatic. No Delta column mapping needed.
  --style fidelity  Original PBI names verbatim (case + spaces preserved).
                    Requires Delta columnMapping for table DDL.

Run:
  dax_to_metric_view.py model.pbit --catalog-schema main.sales --emit-ddl --out scaffold.sql
  dax_to_metric_view.py model.pbit --style fidelity --source main.sales.fact_sales
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

# ----------------------------------------------------------------------------
# Synonyms (mechanical 1:1 mapping from PBI names — no DAX semantics)
# ----------------------------------------------------------------------------
#
# Synonyms are rule-based, not LLM-authored. The mapping from a PBI measure
# name to its DAX bracket form / qualified form is purely lexical and does not
# benefit from LLM judgment.
#
# Comments (the `comment:` field), in contrast, ARE LLM-authored — they
# capture business meaning that the DAX shape doesn't.

def build_measure_synonyms(orig_name: str, metric_view_name: str | None = None) -> list[str]:
    """Synonyms for a migrated measure.

    Always includes the bracketed DAX form (`[Original Name]`) — that's the
    canonical DAX reference and never duplicates `name:`. Also includes the
    bare original PBI name **only when it differs** from the metric-view
    `name:`; otherwise it's redundant with `name:` and adds no discovery value.

    `metric_view_name` defaults to `orig_name` (i.e. no rename), in which case
    only the bracketed form is emitted.
    """
    if metric_view_name is None:
        metric_view_name = orig_name
    syns: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = s.strip()
        if s and s not in seen and s != metric_view_name:
            seen.add(s)
            syns.append(s)

    _add(f"[{orig_name}]")     # canonical DAX bracket form
    if orig_name != metric_view_name:
        _add(orig_name)        # bare original name only when renamed
    return syns


def build_dimension_synonyms(
    orig_table: str,
    orig_col: str,
    dim_name: str,
) -> list[str]:
    """Synonyms for a migrated dimension.

    A PBI column is referenced in DAX two ways:
      1. Bare column name in row context: `[Order Date]`  →  rare, columns
         usually need the table prefix to disambiguate
      2. Qualified form: `'Calendar'[Order Date]`         →  canonical

    For metric-view discovery, the useful synonyms are:
      - The bare PBI column name (when it differs from `dim_name`)
      - The qualified `'Table'[Column]` form (when it differs from `dim_name`)

    `dim_name` is the metric-view `name:` (often a "Table Column" concat for
    join-table dims, or a snake_case-rename for fact dims). Anything matching
    `dim_name` is dropped — it'd be redundant with `name:` itself.
    """
    syns: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = s.strip()
        if s and s not in seen and s != dim_name:
            seen.add(s)
            syns.append(s)

    _add(orig_col)                       # bare column name
    _add(f"'{orig_table}'[{orig_col}]")  # canonical DAX qualified form
    return syns


# ----------------------------------------------------------------------------
# .pbit loader (stdlib-only)
# ----------------------------------------------------------------------------

# DataModelSchema lives at the root of the .pbit ZIP (sometimes under a path
# variant in older PBI versions).
_PBIT_SCHEMA_MEMBER_CANDIDATES = (
    "DataModelSchema",
    "DataModel/DataModelSchema",
)


def _read_datamodel_schema(zf: zipfile.ZipFile) -> dict:
    last_err = None
    for member in _PBIT_SCHEMA_MEMBER_CANDIDATES:
        try:
            raw = zf.read(member)
            break
        except KeyError as e:
            last_err = e
            continue
    else:
        raise SystemExit(
            "DataModelSchema not found in .pbit. Members present:\n  "
            + "\n  ".join(zf.namelist())
        )
    # PBI usually writes UTF-16 LE BOM, sometimes UTF-8.
    for enc in ("utf-16", "utf-16-le", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc)
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise SystemExit("Could not decode DataModelSchema as JSON in any common encoding.")


def _expr_to_string(e) -> str:
    if e is None:
        return ""
    if isinstance(e, str):
        return e
    if isinstance(e, list):
        return "\n".join(_expr_to_string(x) for x in e)
    return str(e)


def _is_calc_table(table: dict) -> bool:
    """Detect calculated-table partitions (DAX `=...` in partition source)."""
    for p in table.get("partitions", []) or []:
        src = (p.get("source") or {}).get("type") or ""
        if str(src).lower() == "calculated":
            return True
    return False


def load_pbit_as_model(path: Path) -> dict:
    """Open a .pbit and return a normalized model dict:
        {tables: [{name, columns:[...], measures:[...]}], relationships: [...]}
    """
    with zipfile.ZipFile(path) as zf:
        root = _read_datamodel_schema(zf)

    model = root.get("model") or {}
    tables_in = model.get("tables") or []

    tables_out: list[dict] = []
    for t in tables_in:
        name = t.get("name")
        if not name:
            continue
        cols_out = []
        for c in t.get("columns", []) or []:
            col_name = c.get("name")
            if not col_name:
                continue
            cols_out.append({
                "name": col_name,
                "dataType": (c.get("dataType") or "string"),
                "isHidden": bool(c.get("isHidden", False)),
                "summarizeBy": (c.get("summarizeBy") or "none"),
                "expression": _expr_to_string(c.get("expression")) if "expression" in c else None,
                "type": c.get("type"),  # e.g. 'calculated'
                "description": _expr_to_string(c.get("description")) if c.get("description") else None,
            })
        measures_out = []
        for m in t.get("measures", []) or []:
            mname = m.get("name")
            if not mname:
                continue
            measures_out.append({
                "name": mname,
                "expression": _expr_to_string(m.get("expression")),
                "description": _expr_to_string(m.get("description")) if m.get("description") else None,
                "_source_table": name,
            })
        tables_out.append({
            "name": name,
            "columns": cols_out,
            "measures": measures_out,
            "isHidden": bool(t.get("isHidden", False)),
            "isCalcTable": _is_calc_table(t),
        })

    rel_out: list[dict] = []
    for r in model.get("relationships", []) or []:
        ft, fc = r.get("fromTable"), r.get("fromColumn")
        tt, tc = r.get("toTable"), r.get("toColumn")
        if not all([ft, fc, tt, tc]):
            continue
        is_active = r.get("isActive")
        rel_out.append({
            "fromTable": ft,
            "fromColumn": fc,
            "toTable": tt,
            "toColumn": tc,
            "isActive": True if is_active is None else bool(is_active),
            "crossFilteringBehavior": r.get("crossFilteringBehavior"),
        })

    return {"tables": tables_out, "relationships": rel_out}


# ----------------------------------------------------------------------------
# Naming style + util
# ----------------------------------------------------------------------------

def _safe(name: str) -> str:
    return f"`{name}`"


def _alias(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower() or "tbl"


def kimball_col(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s or "col"


def kimball_table(name: str, is_fact: bool, fact_suffix: str | None) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "tbl"
    if is_fact:
        if base.startswith("fact_") or base == "fact":
            return base if "_" in base else f"fact_{fact_suffix or 'profitability'}"
        return f"fact_{fact_suffix or base}"
    if base.startswith("dim_"):
        return base
    return f"dim_{base}"


def needs_column_mapping(cols: list[dict]) -> bool:
    return any(re.search(r"[^A-Za-z0-9_]", c["name"]) for c in cols)


# Hidden, auto-generated PBI tables. Auto date/time hierarchies live in these
# placeholders — they have no business data and must be excluded everywhere
# (DDL emit, fact picking, joins, dimensions).
_AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")


def _is_real_table(name: str) -> bool:
    return not name.startswith(_AUTO_DATE_PREFIXES)


# ----------------------------------------------------------------------------
# Fact picking
# ----------------------------------------------------------------------------

def pick_fact_table(tables: list[dict], explicit_fact: str | None) -> dict:
    if explicit_fact:
        for t in tables:
            if t.get("name") == explicit_fact:
                return t
        raise SystemExit(f"--fact-table {explicit_fact!r} not found in model")
    candidates = [t for t in tables if _is_real_table(t["name"])] or tables
    return max(candidates, key=lambda t: (len(t.get("measures", [])), len(t.get("columns", []))))


def detect_fact_date_column(fact: dict, doc: dict) -> str | None:
    """Find the column on the fact table that joins to a calendar dim.
    Returns the (original) column name, or None.

    Used purely as a hint for the agent — the script no longer auto-injects
    snapshot-flag columns into source: SQL. The agent decides whether to add
    them based on which measures use LASTDATE / DATEADD / FIRSTDATE.
    """
    fact_name = fact["name"]
    for r in doc.get("relationships", []):
        if r.get("fromTable") != fact_name or r.get("isActive") is False:
            continue
        to = (r.get("toTable") or "").lower()
        if "calendar" in to or "date" in to:
            return r["fromColumn"]
    for c in fact.get("columns", []):
        n = c["name"].lower()
        dt = (c.get("dataType") or "").lower()
        if ("date" in n or "snapshot" in n) and ("date" in dt or "time" in dt):
            return c["name"]
    for c in fact.get("columns", []):
        dt = (c.get("dataType") or "").lower()
        if "date" in dt or "time" in dt:
            return c["name"]
    return None


# ----------------------------------------------------------------------------
# Metric view scaffold (no DAX translation here — that's the agent's job)
# ----------------------------------------------------------------------------

def build_metric_view(
    doc: dict,
    source_override: str | None,
    fact_override: str | None,
    style: str = "kimball",
    catalog_schema: str | None = None,
    fact_suffix: str | None = None,
) -> tuple[dict, list[str]]:
    """Build the structural scaffold of the metric view.

    Populates source / joins / dimensions / synonyms mechanically. Each measure
    in the output dict carries its name + ORIGINAL DAX (preserved verbatim for
    the agent to translate); `expr:` is left as the placeholder string
    'AGENT_TRANSLATE_DAX' until the agent fills it in.
    """
    tables = [t for t in (doc.get("tables") or []) if _is_real_table(t["name"])]
    if not tables:
        raise SystemExit("No tables in model")

    fact = pick_fact_table(tables, fact_override)
    fact_name = fact["name"]
    relationships = [
        r for r in (doc.get("relationships") or [])
        if _is_real_table(r.get("fromTable", "")) and _is_real_table(r.get("toTable", ""))
    ]

    warnings: list[str] = []
    cs_prefix = catalog_schema if catalog_schema else "<TODO_CATALOG.SCHEMA>"

    def physical_table(name: str) -> str:
        if style == "kimball":
            return kimball_table(name, is_fact=(name == fact_name), fact_suffix=fact_suffix)
        return name

    def physical_col(name: str) -> str:
        return kimball_col(name) if style == "kimball" else name

    def col_ref(name: str) -> str:
        return _safe(physical_col(name))

    def fq_table(name: str) -> str:
        return f"{cs_prefix}.{physical_table(name)}"

    # Joins: only direct fact → dim, active relationships only.
    join_aliases: dict[str, str] = {}
    joins: list[dict] = []
    for r in relationships:
        if r.get("fromTable") != fact_name:
            continue
        if r.get("isActive") is False:
            warnings.append(
                f"inactive relationship to {r['toTable']!r} skipped — "
                f"if a measure needs USERELATIONSHIP, agent must add a second join entry by hand"
            )
            continue
        to_tbl = r["toTable"]
        if to_tbl in join_aliases:
            continue
        alias = _alias(to_tbl)
        join_aliases[to_tbl] = alias
        from_col = physical_col(r["fromColumn"])
        to_col = physical_col(r["toColumn"])
        joins.append({
            "name": alias,
            "source": fq_table(to_tbl),
            "on": f"source.{_safe(from_col)} = {alias}.{_safe(to_col)}",
        })

    # Dimensions: non-numeric / non-summarized fact columns + all join columns.
    dimensions: list[dict] = []

    def _dim_entry(name: str, expr: str, orig_table: str, orig_col: str, pbi_desc: str | None) -> dict:
        d: dict = {
            "name": name,
            "expr": expr,
            "synonyms": build_dimension_synonyms(orig_table, orig_col, name),
        }
        if pbi_desc:
            d["comment"] = pbi_desc.strip()
        return d

    for col in fact.get("columns", []):
        if col.get("isHidden"):
            continue
        dt = (col.get("dataType") or "").lower()
        summarize = (col.get("summarizeBy") or "none").lower()
        is_measure_like = dt in ("decimal", "double", "int64", "currency") and summarize not in ("none", "")
        if is_measure_like:
            continue
        dimensions.append(_dim_entry(
            name=col["name"],
            expr=col_ref(col["name"]),
            orig_table=fact_name,
            orig_col=col["name"],
            pbi_desc=col.get("description"),
        ))

    for t in tables:
        if t["name"] == fact_name or t["name"] not in join_aliases:
            continue
        alias = join_aliases[t["name"]]
        for col in t.get("columns", []):
            if col.get("isHidden"):
                continue
            dimensions.append(_dim_entry(
                name=f"{t['name']} {col['name']}",
                expr=f"{alias}.{col_ref(col['name'])}",
                orig_table=t["name"],
                orig_col=col["name"],
                pbi_desc=col.get("description"),
            ))

    if not dimensions:
        warnings.append("no dimensions discovered; metric view requires at least one")

    # Measures: collect, do NOT translate. Each entry carries the original DAX
    # for the agent to translate. `expr` stays as the AGENT_TRANSLATE_DAX
    # sentinel until the agent fills it in.
    measures: list[dict] = []
    for t in tables:
        for m in t.get("measures", []) or []:
            entry = {
                "name": m["name"],
                "expr": "AGENT_TRANSLATE_DAX",
                "_dax": m.get("expression", "") or "",
                "_source_table": t["name"],
                "synonyms": build_measure_synonyms(m["name"], m["name"]),
            }
            desc = m.get("description")
            if desc:
                entry["comment"] = desc.strip()
            measures.append(entry)

    if not measures:
        warnings.append("no measures discovered; metric view requires at least one")

    # Source: plain table reference. Agent adds snapshot-flag columns
    # (is_latest_snapshot, etc.) as inline SQL when any measure needs them.
    fact_date_hint = detect_fact_date_column(fact, doc)
    source_value = source_override or fq_table(fact_name)

    mv: dict = {
        "version": "1.1",
        "comment": (f"Generated from Power BI .pbit tabular model. "
                    f"Fact table: {physical_table(fact_name)} ({style} style)."),
        "source": source_value,
        "_fact_date_hint": fact_date_hint,  # consumed by emit_yaml for the agent's reference
    }
    if joins:
        mv["joins"] = joins
    mv["dimensions"] = dimensions
    mv["measures"] = measures

    return mv, warnings


# ----------------------------------------------------------------------------
# DDL emission
# ----------------------------------------------------------------------------

_TABULAR_TO_SPARK = {
    "int64": "BIGINT",
    "double": "DOUBLE",
    "decimal": "DECIMAL(38, 4)",
    "currency": "DECIMAL(38, 4)",
    "datetime": "TIMESTAMP",
    "date": "DATE",
    "string": "STRING",
    "boolean": "BOOLEAN",
    "binary": "BINARY",
}


def _spark_type(tabular_type: str | None) -> str:
    return _TABULAR_TO_SPARK.get((tabular_type or "string").lower(), "STRING")


def emit_table_ddl(
    doc: dict,
    catalog_schema: str | None,
    style: str = "kimball",
    fact_table_orig: str | None = None,
    fact_suffix: str | None = None,
) -> str:
    tables = [t for t in (doc.get("tables") or []) if _is_real_table(t["name"])]
    if not tables:
        return ""

    cs_prefix = catalog_schema if catalog_schema else "<TODO_CATALOG.SCHEMA>"
    fact_orig = fact_table_orig or pick_fact_table(tables, None)["name"]

    out: list[str] = []
    out.append(f"-- Schema-only DDL for the {style} layout. Run after CREATE SCHEMA {cs_prefix}.")
    out.append(f"-- Data ingestion is a separate step (Lakeflow Connect, COPY INTO from CSV, etc).")
    out.append("")

    for t in tables:
        cols = t.get("columns", [])
        if not cols:
            continue
        physical = (kimball_table(t["name"], is_fact=(t["name"] == fact_orig), fact_suffix=fact_suffix)
                    if style == "kimball" else t["name"])
        seen_names: dict[str, int] = {}
        col_lines: list[str] = []
        for c in cols:
            name = kimball_col(c["name"]) if style == "kimball" else c["name"]
            if name in seen_names:
                seen_names[name] += 1
                name = f"{name}_{seen_names[name]}"
            else:
                seen_names[name] = 1
            spark_type = _spark_type(c.get("dataType"))
            col_lines.append(f"  `{name}` {spark_type}")
        # IF NOT EXISTS — never replace a table that already holds data.
        # If the user wants to recreate, they must DROP TABLE first explicitly.
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {cs_prefix}.{physical} (\n"
            + ",\n".join(col_lines)
            + "\n)"
        )
        if style == "fidelity" and needs_column_mapping(cols):
            ddl += (
                "\nTBLPROPERTIES ("
                "'delta.columnMapping.mode' = 'name',"
                " 'delta.minReaderVersion' = '2',"
                " 'delta.minWriterVersion' = '5')"
            )
        ddl += f"\nCOMMENT 'From Power BI tabular model: {t['name']} ({style} style)';"
        out.append(ddl)
        out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# YAML emission (scaffold — agent fills measure exprs/comments)
# ----------------------------------------------------------------------------

def _yaml_scalar(v: str) -> str:
    """Quote a value if needed; else emit raw. Conservative — quote when the
    value contains YAML special chars or starts with one."""
    if not v:
        return '""'
    if re.search(r"[:\#\&\*\!\|\>\?\{\}\[\],\"\'%@`]", v) or v[0] in (" ", "-", "?"):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def emit_yaml(mv: dict) -> str:
    out: list[str] = []
    out.append(f"version: \"{mv['version']}\"")
    if mv.get("comment"):
        out.append(f"comment: {_yaml_scalar(mv['comment'])}")
    src = mv["source"]
    if isinstance(src, str) and src.startswith("|"):
        # multi-line literal block (unused by the scaffolder; agent may add one)
        out.append("source: |")
        for line in src.splitlines()[1:]:
            out.append(line)
    else:
        out.append(f"source: {_yaml_scalar(src)}")
    if mv.get("filter"):
        out.append(f"filter: {_yaml_scalar(mv['filter'])}")
    if mv.get("joins"):
        out.append("joins:")
        for j in mv["joins"]:
            out.append(f"  - name: {_yaml_scalar(j['name'])}")
            out.append(f"    source: {_yaml_scalar(j['source'])}")
            out.append(f"    on: {_yaml_scalar(j['on'])}")

    out.append("dimensions:")
    for d in mv["dimensions"]:
        # `comment:` and `synonyms:` on dimensions require the v1.1+ metric-view
        # serde (DBR 17.2+). Older serdes only know `name/expr/window` on dims.
        # If you hit "Unrecognized field 'synonyms' (class … v10.Column)" on
        # CREATE VIEW, re-run with --no-dim-metadata.
        out.append(f"  - name: {_yaml_scalar(d['name'])}")
        out.append(f"    expr: {_yaml_scalar(d['expr'])}")
        if d.get("comment"):
            out.append(f"    comment: {_yaml_scalar(d['comment'])}")
        else:
            out.append(f"    # comment: AGENT_AUTHOR — replace with a one-line business description")
        d_syns = [s for s in (d.get("synonyms") or []) if s and s != d["name"]]
        if d_syns:
            out.append(f"    synonyms:")
            for syn in d_syns:
                out.append(f"      - {_yaml_scalar(syn)}")

    # Measures: scaffold only. The agent fills in expr + comment, replacing the
    # AGENT_TRANSLATE_DAX / AGENT_AUTHOR placeholders.
    out.append("measures:")
    fact_date_hint = mv.get("_fact_date_hint")
    if fact_date_hint:
        out.append(f"  # Fact date column hint (use for snapshot flags if needed): `{fact_date_hint}`")
    out.append(f"  # AGENT INSTRUCTIONS:")
    out.append(f"  #   For each measure below, the original DAX is preserved as a YAML")
    out.append(f"  #   comment block. Replace `AGENT_TRANSLATE_DAX` with the metric-view SQL")
    out.append(f"  #   translation, and `AGENT_AUTHOR` with a one-line business comment.")
    out.append(f"  #   Translation cookbook: references/dax-to-sql-patterns.md")
    out.append(f"  #")
    out.append(f"  #   If any measure uses LASTDATE / FIRSTDATE / DATEADD(...,-1,DAY) as a")
    out.append(f"  #   CALCULATE filter, you MUST also replace the `source:` above with an")
    out.append(f"  #   inline SELECT that adds the corresponding boolean snapshot flag column")
    out.append(f"  #   (is_latest_snapshot, is_first_snapshot, is_yesterday_snapshot).")
    out.append(f"  #   See SKILL.md § Snapshot flags for the standard pattern.")
    n = len(mv["measures"])
    for i, m in enumerate(mv["measures"], start=1):
        src_table = m.get("_source_table") or "?"
        out.append("")
        out.append(f"  # === Measure {i}/{n}: {m['name']} (PBI table: {src_table}) ===")
        dax_lines = (m.get("_dax") or "").splitlines() or [""]
        out.append(f"  # Original DAX:")
        for dl in dax_lines:
            out.append(f"  #   {dl}")
        out.append(f"  - name: {_yaml_scalar(m['name'])}")
        out.append(f"    expr: AGENT_TRANSLATE_DAX")
        if m.get("comment"):
            out.append(f"    comment: {_yaml_scalar(m['comment'])}")
        else:
            out.append(f"    comment: AGENT_AUTHOR")
        syns = [s for s in (m.get("synonyms") or []) if s and s != m["name"]]
        if syns:
            out.append(f"    synonyms:")
            for syn in syns:
                out.append(f"      - {_yaml_scalar(syn)}")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Verification (mechanical checks only — translation correctness is the
# agent's responsibility, validated via the live compile-check SQL block)
# ----------------------------------------------------------------------------

_BACKTICK_REF = re.compile(r"(?:(\w+)\.)?`([^`]+)`")


def verify_static(doc: dict, mv: dict, style: str, fact_orig: str) -> list[str]:
    """Static schema check on the dimensions only. Measure exprs are agent-filled
    placeholders at scaffold time, so we can't verify them statically here —
    use --emit-verify-sql for that, which runs after the agent fills them in."""
    issues: list[str] = []

    def physical(name: str) -> str:
        return kimball_col(name) if style == "kimball" else name

    fact = next((t for t in doc.get("tables", []) if t["name"] == fact_orig), None)
    fact_cols: set[str] = set()
    if fact:
        fact_cols = {physical(c["name"]) for c in fact.get("columns", [])}
        # Snapshot flags the agent may add — known ok.
        fact_cols.update({"is_latest_snapshot", "is_first_snapshot", "is_yesterday_snapshot"})

    join_aliases: dict[str, str] = {}
    for j in mv.get("joins", []):
        physical_table_name = j["source"].rsplit(".", 1)[-1]
        for t in doc.get("tables", []):
            phys = (kimball_table(t["name"], is_fact=(t["name"] == fact_orig), fact_suffix=None)
                    if style == "kimball" else t["name"])
            if phys == physical_table_name or (phys.startswith("fact_") and physical_table_name.startswith("fact_")):
                join_aliases[j["name"]] = t["name"]
                break

    join_cols: dict[str, set[str]] = {}
    for alias, orig_name in join_aliases.items():
        t = next((tt for tt in doc.get("tables", []) if tt["name"] == orig_name), None)
        if t:
            join_cols[alias] = {physical(c["name"]) for c in t.get("columns", [])}

    def check_expr(expr: str, label: str) -> None:
        for mt in _BACKTICK_REF.finditer(expr):
            alias, col = mt.group(1), mt.group(2)
            if alias:
                if alias not in join_cols:
                    issues.append(f"{label}: alias '{alias}' has no join in YAML (in `{alias}.{col}`)")
                elif col not in join_cols[alias]:
                    issues.append(f"{label}: '{alias}.{col}' — column not on joined table")
            else:
                if col not in fact_cols:
                    issues.append(f"{label}: '`{col}`' — column not on fact table {fact_orig!r}")

    for d in mv.get("dimensions", []):
        check_expr(d.get("expr", ""), f"dim {d['name']!r}")
    return issues


def verify_structural(doc: dict, mv: dict, fact_orig: str) -> list[str]:
    issues: list[str] = []
    src_real_tables = [t for t in doc.get("tables", []) if _is_real_table(t["name"])]
    src_measure_count = sum(len(t.get("measures", [])) for t in src_real_tables)
    src_active_rels_from_fact = [
        r for r in doc.get("relationships", [])
        if r.get("fromTable") == fact_orig and r.get("isActive") is not False
        and r.get("toTable") and _is_real_table(r["toTable"])
    ]
    yaml_measures = len(mv.get("measures", []))
    yaml_joins = len(mv.get("joins", []))
    if yaml_measures != src_measure_count:
        issues.append(f"measure count mismatch: PBIT has {src_measure_count}, YAML has {yaml_measures}")
    if yaml_joins != len(src_active_rels_from_fact):
        issues.append(
            f"join count mismatch: PBIT has {len(src_active_rels_from_fact)} active rels from "
            f"{fact_orig!r}, YAML has {yaml_joins}"
        )
    return issues


def emit_verify_sql(mv: dict, view_full_name: str) -> str:
    """Verification SQL — commented out by default. The agent should uncomment
    after filling in measure exprs, then run via execute_sql to live-compile-check
    every measure against the deployed view."""
    lines: list[str] = []
    lines.append("-- ============================================================")
    lines.append("-- VERIFICATION (LIVE COMPILE CHECK)")
    lines.append("--")
    lines.append("-- Uncomment AFTER the agent has filled in every measure's `expr:`.")
    lines.append("-- Run via execute_sql to catch:")
    lines.append("--   • MEASURE() FILTER (WHERE) — engine rejects this; inline the agg")
    lines.append("--   • BINARY_OP_DIFF_TYPES — INTERVAL '-1' YEAR with INT order dim")
    lines.append("--   • Unresolved column refs / missing join aliases")
    lines.append("-- ============================================================")
    lines.append("")
    lines.append(f"-- DESCRIBE EXTENDED {view_full_name};")
    lines.append("")
    for m in mv.get("measures", []):
        safe_name = m["name"].replace("`", "``")
        lines.append(f"-- {m['name']}")
        lines.append(f"-- SELECT MEASURE(`{safe_name}`) AS m FROM {view_full_name} LIMIT 1;")
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Scaffold a Databricks UC metric view from a Power BI .pbit. "
                    "Agent translates DAX→SQL; this script does the structural plumbing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (kimball default — Genie/dbt idiomatic):\n"
            "  dax_to_metric_view.py model.pbit --catalog-schema main.sales --emit-ddl --out scaffold.sql\n"
            "  dax_to_metric_view.py model.pbit --catalog-schema main.sales --fact-suffix profitability\n"
            "\n"
            "Source-fidelity (preserve PBI names verbatim — needs Delta column mapping):\n"
            "  dax_to_metric_view.py model.pbit --style fidelity --source main.sales.fact_sales\n"
            "\n"
            "Output is a SCAFFOLD: dimensions/joins/synonyms are populated mechanically;\n"
            "each measure has the original DAX preserved as YAML comments and\n"
            "expr: AGENT_TRANSLATE_DAX / comment: AGENT_AUTHOR placeholders for the\n"
            "agent (LLM) running this skill to fill in. See SKILL.md for the workflow.\n"
            "\n"
            "Get a .pbit from a .pbix: open in PBI Desktop → File → Export → Power BI Template.\n"
        ),
    )
    ap.add_argument("input", help="Path to a .pbit file (Power BI Template)")
    ap.add_argument("--style", choices=["kimball", "fidelity"], default="kimball")
    ap.add_argument("--catalog-schema",
                    help="catalog.schema prefix (e.g. 'main.sales'). Required for runnable SQL.")
    ap.add_argument("--source",
                    help="Override the metric-view source (catalog.schema.table or full SQL).")
    ap.add_argument("--fact-table", help="Override fact-table selection (default: most measures)")
    ap.add_argument("--fact-suffix", help="Domain suffix for kimball fact-table name")
    ap.add_argument("--emit-ddl", action="store_true",
                    help="Emit CREATE TABLE DDL for every table (schema-only) before the metric view.")
    ap.add_argument("--out", help="Write to file (default: stdout)")
    ap.add_argument("--verify", action="store_true",
                    help="Run static schema check (dimensions only) + structural diff.")
    ap.add_argument("--emit-verify-sql", action="store_true",
                    help="Append a verification SQL block (commented out) — agent uncomments after filling exprs.")
    ap.add_argument("--no-dim-metadata", action="store_true",
                    help=("Strip `comment:` and `synonyms:` from every dimension before "
                          "emitting the YAML. Use this if your DBR is < 17.2 — the older "
                          "metric-view serde rejects dim comment/synonyms with "
                          "`Unrecognized field 'synonyms' (class … v10.Column)`. Measures "
                          "still get full metadata."))
    args = ap.parse_args(argv)

    path = Path(args.input)
    if path.suffix.lower() != ".pbit":
        raise SystemExit(
            f"Only .pbit is supported. Got: {path.suffix!r}.\n"
            "Get a .pbit from a .pbix: open in PBI Desktop → File → Export → Power BI Template.\n"
            "See references/extracting-pbi-models.md for other source formats."
        )

    doc = load_pbit_as_model(path)
    fact_orig = pick_fact_table(doc.get("tables") or [], args.fact_table)["name"]

    mv, warnings = build_metric_view(
        doc,
        source_override=args.source,
        fact_override=args.fact_table,
        style=args.style,
        catalog_schema=args.catalog_schema,
        fact_suffix=args.fact_suffix,
    )

    parts: list[str] = []
    if args.emit_ddl:
        parts.append(emit_table_ddl(
            doc,
            catalog_schema=args.catalog_schema,
            style=args.style,
            fact_table_orig=fact_orig,
            fact_suffix=args.fact_suffix,
        ))

    if args.no_dim_metadata:
        for d in mv.get("dimensions", []):
            d.pop("comment", None)
            d.pop("synonyms", None)
    yaml_body = emit_yaml(mv)
    cs = args.catalog_schema if args.catalog_schema else "<TODO_CATALOG.SCHEMA>"
    fact_phys = (kimball_table(fact_orig, is_fact=True, fact_suffix=args.fact_suffix)
                 if args.style == "kimball" else fact_orig)
    metric_view_name = f"{cs}.{fact_phys}_metrics"
    parts.append(
        f"-- =============================================================\n"
        f"-- AGENT: this YAML is a SCAFFOLD. Replace every AGENT_TRANSLATE_DAX\n"
        f"-- with the metric-view SQL translation of the original DAX (preserved\n"
        f"-- as YAML comments above each measure). Replace each AGENT_AUTHOR\n"
        f"-- with a one-line business-meaning comment.\n"
        f"-- See references/dax-to-sql-patterns.md for the cookbook.\n"
        f"-- =============================================================\n"
        f"CREATE OR REPLACE VIEW {metric_view_name}\n"
        f"WITH METRICS\n"
        f"LANGUAGE YAML\n"
        f"AS $$\n{yaml_body}$$;\n"
    )

    if args.emit_verify_sql:
        parts.append(emit_verify_sql(mv, metric_view_name))

    output = "\n".join(parts)
    if args.out:
        Path(args.out).write_text(output)
    else:
        sys.stdout.write(output)

    static_issues: list[str] = []
    structural_issues: list[str] = []
    if args.verify:
        static_issues = verify_static(doc, mv, args.style, fact_orig)
        structural_issues = verify_structural(doc, mv, fact_orig)
        print("\n=== Verification ===", file=sys.stderr)
        src_real_tables = [t for t in doc.get("tables", []) if _is_real_table(t["name"])]
        src_measures = sum(len(t.get("measures", [])) for t in src_real_tables)
        print(f"  source: {len(src_real_tables)} tables, {src_measures} measures", file=sys.stderr)
        print(f"  output: {len(mv.get('dimensions', []))} dimensions, "
              f"{len(mv.get('measures', []))} measures (all AGENT_TRANSLATE_DAX), "
              f"{len(mv.get('joins', []))} joins", file=sys.stderr)
        print(f"  Static schema check (dimensions only): {len(static_issues)} issue(s)", file=sys.stderr)
        for i in static_issues[:20]:
            print(f"    • {i}", file=sys.stderr)
        if len(static_issues) > 20:
            print(f"    ... +{len(static_issues) - 20} more", file=sys.stderr)
        print(f"  Structural diff: {len(structural_issues)} issue(s)", file=sys.stderr)
        for i in structural_issues:
            print(f"    • {i}", file=sys.stderr)
        if args.emit_verify_sql:
            print(f"  Live compile check: verify SQL appended (commented out — agent uncomments after translation).",
                  file=sys.stderr)

    if warnings:
        print("\n=== Scaffolder warnings ===", file=sys.stderr)
        for w in warnings:
            print(f"  • {w}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
