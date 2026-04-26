#!/usr/bin/env python3
"""
Convert a Power BI .pbit (Power BI Template) into a Databricks UC metric view
plus optional CREATE TABLE DDL.

Reads .pbit (ZIP containing DataModelSchema JSON / TMSL) using only the Python
stdlib — no third-party deps. The .pbit format exposes the FULL tabular model
schema (every column, every relationship, every M-loaded base column) as JSON,
unlike .pbix where the same data lives in an XPRESS9-compressed binary that
community readers like pbixray decode incompletely.

The tool:
  1. Parses tables, columns (incl. calculated), measures (DAX), relationships.
  2. Picks the table with the most measures as the fact (overrideable).
  3. Builds star-schema joins from active relationships.
  4. Translates each DAX measure to SQL — substantially more coverage than
     the previous .pbix-based version. Highlights:
       - LASTDATE/FIRSTDATE → FILTER (WHERE is_latest_snapshot) + auto-augments
         the source SQL with the boolean flag column.
       - DATEADD( ..., -1, DAY/MONTH/...) → flagged with a LAG-column suggestion;
         emits a TODO and the matching LAG into the SQL source so the consumer
         only has to wire the measure expr.
       - VAR / RETURN inlined when bindings are pure expressions.
       - SWITCH(TRUE(), cond, val, ..., default) → CASE WHEN cond THEN val.
       - IFERROR(a, b) → a (rely on NULLIF in DIVIDE; fallback dropped).
       - BLANK() → NULL.
       - FORMAT(x, "...") → x (format strings are consumer-side).
       - SELECTEDVALUE/ISFILTERED IF-wrappers (UI edge cases) unwrapped.
       - String concat & → ||.
       - Power 10^N → literal (1000000 etc.).
       - Bare [Measure] refs → MEASURE(`Measure`) (or `column` if unknown).
       - Forward MEASURE() refs detected and inlined to keep the YAML
         createable in one shot (Databricks metric views require backward refs).
       - FILTER('T', expr) inside CALCULATE → unwrapped to the bare predicate.
  5. Emits schema-only DDL + the metric view CREATE VIEW. Never INSERTs.

Two naming styles:
  --style kimball   (default) lowercase snake_case, dim_*/fact_* prefixes.
                    Genie / dbt / Fabric idiomatic. No Delta column mapping needed.
  --style fidelity  Original PBI names verbatim (case + spaces preserved).
                    Requires Delta columnMapping for table DDL.

Run:
  dax_to_metric_view.py model.pbit --catalog-schema main.sales --emit-ddl --out out.sql
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
# Balanced-paren / arg-splitting utilities
# ----------------------------------------------------------------------------

def split_top_level_args(s: str) -> list[str]:
    """Split a comma-separated arg list, respecting parens, brackets, braces, and quoted strings.

    DAX `IN {"A","B"}` set literals must NOT be split — track `{`/`}` depth.
    """
    out, cur, depth, in_str, str_ch = [], [], 0, False, ""
    for c in s:
        if in_str:
            cur.append(c)
            if c == str_ch:
                in_str = False
            continue
        if c in ('"', "'"):
            in_str = True
            str_ch = c
            cur.append(c)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    if cur:
        out.append("".join(cur).strip())
    return out


def find_call(expr: str, fname: str) -> tuple[int, int, str] | None:
    """Find the next call to FNAME(...) and return (start, end, args_string)."""
    pat = re.compile(r"\b" + re.escape(fname) + r"\s*\(", re.IGNORECASE)
    m = pat.search(expr)
    if not m:
        return None
    start = m.start()
    i = m.end()
    depth = 1
    in_str, str_ch = False, ""
    while i < len(expr) and depth:
        c = expr[i]
        if in_str:
            if c == str_ch:
                in_str = False
        elif c in ('"', "'"):
            in_str = True
            str_ch = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return None
    return start, i, expr[m.end() : i - 1]


def _replace_calls(s: str, fname: str, build_replacement) -> str:
    """Walk every call to FNAME(...) and rewrite it via build_replacement(args).
    Forward-advancing cursor; safe even when the SQL name matches the DAX name."""
    offset = 0
    while True:
        hit = find_call(s[offset:], fname)
        if not hit:
            return s
        start, end, inner = hit
        args = split_top_level_args(inner) if inner else []
        repl = build_replacement(args)
        if repl is None:
            offset += end
            continue
        abs_start = offset + start
        abs_end = offset + end
        s = s[:abs_start] + repl + s[abs_end:]
        offset = abs_start + len(repl)


# ----------------------------------------------------------------------------
# DAX preprocessing
# ----------------------------------------------------------------------------

_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_POWER_LITERAL_RE = re.compile(r"\b10\s*\^\s*(\d+)\b")  # 10^6 → 1000000


def strip_dax_comments(s: str) -> str:
    s = _BLOCK_COMMENT_RE.sub(" ", s)
    s = _LINE_COMMENT_RE.sub("", s)
    return s


def expand_power_literals(s: str) -> str:
    """`10^6` → `1000000`. Catches the very common DAX-/-1M divisor idiom."""
    def rep(m: re.Match) -> str:
        n = int(m.group(1))
        return str(10 ** n)
    return _POWER_LITERAL_RE.sub(rep, s)


# ----------------------------------------------------------------------------
# DAX -> SQL atomic translations
# ----------------------------------------------------------------------------

DAX_STRING_LITERAL_RE = re.compile(r'"([^"]*)"')


def _drop_table_qualifier(s: str) -> str:
    """'Sales'[Amount] -> `Amount`,  Sales[Amount] -> `Amount`."""
    s = re.sub(r"'(?:[^']+)'\[([^\]]+)\]", r"`\1`", s)
    s = re.sub(r"\b[A-Za-z_]\w*\[([^\]]+)\]", r"`\1`", s)
    return s


def _convert_string_literals(s: str) -> str:
    def rep(m: re.Match) -> str:
        return "'" + m.group(1).replace("'", "''") + "'"
    return DAX_STRING_LITERAL_RE.sub(rep, s)


def _translate_logical_ops(s: str) -> str:
    s = re.sub(r"&&", " AND ", s)
    s = re.sub(r"\|\|", " OR ", s)
    return s


def _translate_string_concat(s: str) -> str:
    """DAX `&` (string concat) → SQL `||`. Skip `&&` and `&=`."""
    return re.sub(r"(?<![&|])&(?!&)", " || ", s)


def _translate_blank(s: str) -> str:
    return re.sub(r"\bBLANK\s*\(\s*\)", "NULL", s, flags=re.IGNORECASE)


def _translate_truefalse(s: str) -> str:
    s = re.sub(r"\bTRUE\s*\(\s*\)", "TRUE", s, flags=re.IGNORECASE)
    s = re.sub(r"\bFALSE\s*\(\s*\)", "FALSE", s, flags=re.IGNORECASE)
    return s


_SIMPLE_AGGS = {
    "SUM": ("SUM", 1),
    "AVERAGE": ("AVG", 1),
    "MIN": ("MIN", 1),
    "MAX": ("MAX", 1),
    "COUNT": ("COUNT", 1),
    "COUNTA": ("COUNT", 1),
    "DISTINCTCOUNT": ("COUNT_DISTINCT", 1),
}


def _translate_simple_aggs(s: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    for dax_name, (sql_name, _argc) in _SIMPLE_AGGS.items():
        def make_repl(args, dax_name=dax_name, sql_name=sql_name):
            if len(args) != 1:
                warnings.append(f"{dax_name}() with {len(args)} args left for manual review")
                return None
            col = _drop_table_qualifier(args[0])
            if sql_name == "COUNT_DISTINCT":
                return f"COUNT(DISTINCT {col})"
            return f"{sql_name}({col})"
        s = _replace_calls(s, dax_name, make_repl)
    return s, warnings


def _translate_countrows(s: str) -> str:
    return _replace_calls(s, "COUNTROWS", lambda args: "COUNT(1)")


def _translate_divide(s: str) -> str:
    def repl(args):
        if len(args) < 2:
            return None
        return f"({args[0]}) / NULLIF(({args[1]}), 0)"
    return _replace_calls(s, "DIVIDE", repl)


def _translate_format(s: str) -> str:
    """FORMAT(expr, "..."): drop the format string, keep expr (formatting is consumer-side)."""
    def repl(args):
        if not args:
            return None
        return args[0]
    return _replace_calls(s, "FORMAT", repl)


def _translate_iferror(s: str) -> str:
    """IFERROR(a, b) → a. Most IFERROR uses in DAX guard against /0; we already use NULLIF."""
    def repl(args):
        if not args:
            return None
        return args[0] if len(args) == 1 else args[0]  # explicit
    return _replace_calls(s, "IFERROR", repl)


def _translate_concatenate(s: str) -> str:
    def repl(args):
        if len(args) < 2:
            return None
        return "concat(" + ", ".join(args) + ")"
    return _replace_calls(s, "CONCATENATE", repl)


def _translate_if(s: str) -> str:
    def repl(args):
        if len(args) == 2:
            return f"CASE WHEN {args[0]} THEN {args[1]} END"
        if len(args) == 3:
            return f"CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END"
        return None
    return _replace_calls(s, "IF", repl)


def _translate_switch(s: str) -> str:
    """SWITCH(target, v1, r1, ..., default).
    Special-case SWITCH(TRUE(), cond1, val1, ..., default) → CASE WHEN cond1 ... END.
    """
    def repl(args):
        if len(args) < 3:
            return None
        target, rest = args[0], args[1:]
        default = None
        if len(rest) % 2 == 1:
            default = rest[-1]
            rest = rest[:-1]
        # Detect SWITCH(TRUE(), ...) — the conditions are boolean themselves.
        is_true_switch = re.match(r"\s*TRUE\s*(\(\s*\))?\s*$", target, re.IGNORECASE) is not None
        if is_true_switch:
            whens = [f"WHEN {rest[i]} THEN {rest[i + 1]}" for i in range(0, len(rest), 2)]
        else:
            whens = [f"WHEN {target} = {rest[i]} THEN {rest[i + 1]}" for i in range(0, len(rest), 2)]
        out = "CASE " + " ".join(whens)
        if default is not None:
            out += f" ELSE {default}"
        return out + " END"
    return _replace_calls(s, "SWITCH", repl)


# ----------------------------------------------------------------------------
# CALCULATE / FILTER / LASTDATE coordination
# ----------------------------------------------------------------------------
#
# CALCULATE(expr, filter1, filter2, ...) becomes "expr FILTER (WHERE f1 AND f2)".
# - FILTER('Table', cond)  →  cond
# - LASTDATE('Cal'[Date])  →  is_latest_snapshot   (and we mark the measure
#                              as needing the source-SQL augmentation)
# - FIRSTDATE('Cal'[Date]) →  is_first_snapshot   (same shape)
#

# Set by translate_dax_expr; reset per measure. Side-channel for letting the
# post-processor know which measures need source augmentation.
_TRANSLATION_FLAGS: dict[str, bool] = {}


_DATEADD_DAY_NEG1_RE = re.compile(
    r"^\s*DATEADD\s*\(\s*(.*?)\s*,\s*-\s*1\s*,\s*DAY\s*\)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _filter_arg_to_sql(arg: str) -> str:
    """One filter arg of CALCULATE → SQL boolean predicate."""
    s = arg.strip()

    # FILTER('T', cond) → cond
    mf = find_call(s, "FILTER")
    if mf and mf[0] == 0 and mf[1] == len(s):
        fargs = split_top_level_args(mf[2])
        if len(fargs) >= 2:
            return _filter_arg_to_sql(",".join(fargs[1:]))

    # DATEADD(LASTDATE('Cal'[Date]), -1, DAY) → is_yesterday_snapshot
    # DATEADD('Cal'[Date], -1, DAY)            → is_yesterday_snapshot (when used
    # as a filter, this means "yesterday's snapshot row" — same shape as latest).
    mda = _DATEADD_DAY_NEG1_RE.match(s)
    if mda:
        _TRANSLATION_FLAGS["needs_yesterday_snapshot"] = True
        return "is_yesterday_snapshot"

    # LASTDATE('Cal'[Date]) → is_latest_snapshot
    ml = find_call(s, "LASTDATE")
    if ml and ml[0] == 0 and ml[1] == len(s):
        _TRANSLATION_FLAGS["needs_latest_snapshot"] = True
        return "is_latest_snapshot"

    # FIRSTDATE('Cal'[Date]) → is_first_snapshot
    mfs = find_call(s, "FIRSTDATE")
    if mfs and mfs[0] == 0 and mfs[1] == len(s):
        _TRANSLATION_FLAGS["needs_first_snapshot"] = True
        return "is_first_snapshot"

    # Otherwise translate as an expression
    sql, _w = translate_dax_expr_inner(s)
    return sql


_AGG_RE = re.compile(
    r"\b(SUM|AVG|MIN|MAX|COUNT|COUNT_DISTINCT)\s*\(", re.IGNORECASE
)


def _attach_filter_to_aggregates(body: str, where: str) -> str:
    """Attach `FILTER (WHERE <where>)` to every aggregate in `body`.

    SQL `FILTER (WHERE ...)` is a clause on the aggregate function call, NOT on
    the surrounding arithmetic. So `CALCULATE(SUM(x)/10^6, p)` must become
    `SUM(x) FILTER (WHERE p) / 1000000`, not `SUM(x) / 1000000 FILTER (WHERE p)`.

    We find each aggregate-function call by name (`SUM(`, `AVG(`, …, including
    the COUNT(DISTINCT …) shape produced by _translate_simple_aggs) and append
    ` FILTER (WHERE <where>)` immediately after its matching closing paren.
    """
    if not where:
        return body
    out_parts: list[str] = []
    cursor = 0
    while True:
        m = _AGG_RE.search(body, cursor)
        if not m:
            out_parts.append(body[cursor:])
            break
        # Already followed by FILTER? Skip — caller is reapplying.
        # We still need to AND the new predicate into the existing WHERE.
        agg_start = m.start()
        # find matching close paren for the (
        depth = 1
        i = m.end()
        in_str, str_ch = False, ""
        while i < len(body) and depth > 0:
            c = body[i]
            if in_str:
                if c == str_ch:
                    in_str = False
                i += 1
                continue
            if c in ("'", '"'):
                in_str = True
                str_ch = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        if depth != 0:
            # malformed — bail and emit the rest unchanged
            out_parts.append(body[cursor:])
            break
        # i is now position right after the matching ).
        # Check if there's already a FILTER (WHERE ...) clause.
        rest = body[i:].lstrip()
        leading_ws = body[i:i + (len(body[i:]) - len(rest))]
        if rest.upper().startswith("FILTER"):
            # find paren after FILTER
            fp = rest.find("(")
            if fp != -1:
                fdepth = 1
                j = fp + 1
                in_str2, str_ch2 = False, ""
                while j < len(rest) and fdepth > 0:
                    c = rest[j]
                    if in_str2:
                        if c == str_ch2:
                            in_str2 = False
                        j += 1
                        continue
                    if c in ("'", '"'):
                        in_str2 = True
                        str_ch2 = c
                    elif c == "(":
                        fdepth += 1
                    elif c == ")":
                        fdepth -= 1
                    j += 1
                # rest[fp+1:j-1] is the FILTER inner; expect "WHERE <pred>"
                inner = rest[fp + 1:j - 1].strip()
                inner_up = inner.upper()
                if inner_up.startswith("WHERE"):
                    existing_pred = inner[5:].strip()
                    new_pred = f"({existing_pred}) AND ({where})"
                    new_clause = f"FILTER (WHERE {new_pred})"
                    out_parts.append(body[cursor:i])
                    out_parts.append(leading_ws + new_clause)
                    cursor = i + len(leading_ws) + (j)  # past the original FILTER (...)
                    continue
        # No existing FILTER — attach a new one.
        out_parts.append(body[cursor:i])
        out_parts.append(f" FILTER (WHERE {where})")
        cursor = i
    return "".join(out_parts)


def _translate_calculate(s: str) -> str:
    def repl(args):
        if not args:
            return None
        body = translate_dax_expr_inner(args[0])[0]
        filters: list[str] = []
        for f in args[1:]:
            filters.append(_filter_arg_to_sql(f))
        if not filters:
            return body
        where = " AND ".join(filters)
        # If the body contains aggregates, attach FILTER to each aggregate so
        # surrounding arithmetic (e.g. /10^6) stays outside the FILTER clause.
        if _AGG_RE.search(body):
            return _attach_filter_to_aggregates(body, where)
        # No aggregate found — fall back to old behavior (rare).
        return f"{body} FILTER (WHERE {where})"
    return _replace_calls(s, "CALCULATE", repl)


# ----------------------------------------------------------------------------
# VAR/RETURN inlining
# ----------------------------------------------------------------------------

# Match: VAR <name> = <expr>  followed by VAR or RETURN (case-insensitive).
# We do this by walking the string and tracking parens depth so we don't split
# inside a function call. The body of each VAR is the text up to the next
# top-level VAR/RETURN keyword.

_VAR_KEYWORD = re.compile(r"\bVAR\b", re.IGNORECASE)
_RETURN_KEYWORD = re.compile(r"\bRETURN\b", re.IGNORECASE)


def _scan_top_level_keyword(s: str, pat: re.Pattern, start: int = 0) -> int | None:
    """Find the next match of pat at parens-depth zero, starting at `start`."""
    depth = 0
    in_str, str_ch = False, ""
    i = start
    while i < len(s):
        c = s[i]
        if in_str:
            if c == str_ch:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            str_ch = c
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if depth == 0:
            m = pat.match(s, i)
            if m:
                return m.start()
        i += 1
    return None


def inline_var_return(s: str) -> str | None:
    """If `s` is of the shape `VAR x = e1 [VAR y = e2 ...] RETURN body`,
    inline each VAR into the body and return the resulting expression.
    Returns None if the pattern doesn't match cleanly."""
    var_pos = _scan_top_level_keyword(s, _VAR_KEYWORD, 0)
    if var_pos is None:
        return None
    # collect (name, expr_text) pairs in order
    bindings: list[tuple[str, str]] = []
    cursor = var_pos
    while True:
        # Match VAR <name> = <expr>
        m = re.match(r"\s*VAR\s+([A-Za-z_]\w*)\s*=\s*", s[cursor:], re.IGNORECASE)
        if not m:
            break
        name = m.group(1)
        expr_start = cursor + m.end()
        # Find the next top-level VAR or RETURN keyword
        nxt_var = _scan_top_level_keyword(s, _VAR_KEYWORD, expr_start)
        nxt_ret = _scan_top_level_keyword(s, _RETURN_KEYWORD, expr_start)
        candidates = [p for p in (nxt_var, nxt_ret) if p is not None]
        if not candidates:
            return None
        end = min(candidates)
        bindings.append((name, s[expr_start:end].strip().rstrip(",").strip()))
        cursor = end
        if nxt_ret is not None and end == nxt_ret:
            break
    # Now match RETURN <body>
    rm = re.match(r"\s*RETURN\s+", s[cursor:], re.IGNORECASE)
    if not rm:
        return None
    body = s[cursor + rm.end():].strip()
    # Inline bindings into body. Substitute each VAR name with `(expr)`.
    # Use word-boundary matching.
    inlined = body
    for name, expr in reversed(bindings):
        # Substitute references — but don't substitute inside other VAR exprs
        # (we already iterate in reverse so later VARs' refs in earlier exprs
        # are handled by chained substitution).
        wrapped = "(" + expr + ")"
        inlined = re.sub(r"\b" + re.escape(name) + r"\b", wrapped, inlined)
        # Also propagate through earlier bindings (chain)
        bindings_resolved = []
        for n2, e2 in bindings:
            if n2 == name:
                bindings_resolved.append((n2, e2))
            else:
                bindings_resolved.append((n2, re.sub(r"\b" + re.escape(name) + r"\b", wrapped, e2)))
        bindings = bindings_resolved
    return inlined.strip()


# ----------------------------------------------------------------------------
# SELECTEDVALUE / ISFILTERED IF-wrapper unwrap
# ----------------------------------------------------------------------------
#
# Many measures look like:
#   IF(SELECTEDVALUE(...) = 'X' && [foo]<>BLANK(), "-",
#   IF(NOT SELECTEDVALUE(...) IN {"A","B"} && [foo]<>BLANK(), "-",
#       <core math>))
# These are UI edge-case display rules driven by slicers. The "core math"
# is what we want; the slicer-based edges have no clean SQL analog without a
# query-time slicer parameter. Strategy: for each top-level IF where the
# condition contains SELECTEDVALUE/ISFILTERED/HASONEVALUE and the THEN branch
# is a literal placeholder ("-", "", or BLANK), drop the IF and keep the ELSE.

_SLICER_FUNCS_RE = re.compile(
    r"\b(?:SELECTEDVALUE|ISFILTERED|HASONEVALUE|HASONEFILTER|ISCROSSFILTERED)\b",
    re.IGNORECASE,
)
# Conditions like `Logic=BLANK()`, `MEASURE(\`X\`)<>BLANK()`, or after translation
# `Logic IS NULL`, `MEASURE(...) IS NOT NULL` are pure BLANK-fallback display
# wrappers — common in Power BI `IF(<core>=BLANK(), "-", <core>)`-style measures.
_BLANK_CHECK_RE = re.compile(
    r"(?:=\s*BLANK\s*\(\s*\)|<>\s*BLANK\s*\(\s*\)|=\s*NULL\b|<>\s*NULL\b|"
    r"\bIS\s+NULL\b|\bIS\s+NOT\s+NULL\b)",
    re.IGNORECASE,
)


def _is_placeholder_value(s: str) -> bool:
    s = s.strip()
    if s in ('"-"', '""', "BLANK()", "BLANK ()", "NULL", "0"):
        return True
    if re.match(r"^'[-\s]?'$", s):
        return True
    return False


def _strip_outer_parens(s: str) -> str:
    """Strip a single layer of outer balanced parens, repeatedly."""
    while True:
        s2 = s.strip()
        if len(s2) >= 2 and s2[0] == "(" and s2[-1] == ")":
            depth = 0
            paired = True
            for i, c in enumerate(s2):
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                if depth == 0 and i < len(s2) - 1:
                    paired = False
                    break
            if paired:
                s = s2[1:-1]
                continue
        return s2


def _exprs_equal(a: str, b: str) -> bool:
    """Loose equality: ignore whitespace and outer parens."""
    return _strip_outer_parens(a).replace(" ", "") == _strip_outer_parens(b).replace(" ", "")


def unwrap_slicer_if(s: str) -> str:
    """Repeatedly strip UI-display IF wrappers. Handled patterns:

      A) IF(<slicer-cond>, "-", real)               → real
      B) IF(<slicer-cond>, BLANK(), real)            → real
      C) IF(<expr> = BLANK(), "-", <expr>)           → <expr>          (or = NULL / IS NULL)
      D) IF(<expr> <> BLANK(), <expr>, "-")          → <expr>          (or <> NULL / IS NOT NULL)
      E) IF(<expr> = BLANK() && <other>, "-", <expr>)→ <expr>
      F) IF(<expr> <> BLANK() && <other>, <expr>, "-")→ <expr>
      G) IF(<cond>, "-", IF(<cond>, "-", real))      → real            (recursive)

    The unwrap recurses into a leading ELSE branch automatically because the
    loop reapplies on the new outer expression. Strips outer parens so that
    e.g. `(IF(slicer, "-", real))` is also unwrapped.
    """
    prev = None
    while prev != s:
        prev = s
        s = _strip_outer_parens(s)
        ifm = find_call(s, "IF")
        if not ifm or ifm[0] != 0 or ifm[1] != len(s):
            break
        args = split_top_level_args(ifm[2])
        if len(args) < 2:
            break
        cond = args[0].strip()
        then_branch = args[1].strip()
        else_branch = args[2].strip() if len(args) > 2 else ""

        slicer = bool(_SLICER_FUNCS_RE.search(cond))
        blank_chk = bool(_BLANK_CHECK_RE.search(cond))

        # A/B/G: slicer-cond + placeholder THEN
        if slicer and _is_placeholder_value(then_branch):
            s = else_branch if else_branch else "NULL"
            continue
        # C/E: blank-check + placeholder THEN + ELSE matches the blank-checked expr
        if blank_chk and _is_placeholder_value(then_branch) and else_branch:
            s = else_branch
            continue
        # D/F: blank-check + placeholder ELSE + THEN matches the checked expr
        if blank_chk and _is_placeholder_value(else_branch) and then_branch:
            s = then_branch
            continue
        # If both branches are equal, just return either.
        if then_branch and else_branch and _exprs_equal(then_branch, else_branch):
            s = then_branch
            continue
        break
    return s.strip()


# ----------------------------------------------------------------------------
# Time-intel detection (LASTDATE / DATEADD / SAMEPERIODLASTYEAR)
# ----------------------------------------------------------------------------

_TIME_INTEL_FUNCS = ("DATEADD", "TOTALYTD", "TOTALMTD", "TOTALQTD",
                     "SAMEPERIODLASTYEAR", "PARALLELPERIOD", "PREVIOUSDAY",
                     "PREVIOUSMONTH", "PREVIOUSQUARTER", "PREVIOUSYEAR",
                     "NEXTDAY", "NEXTMONTH", "NEXTQUARTER", "NEXTYEAR")


def detect_time_intel(s: str) -> list[str]:
    found = []
    for f in _TIME_INTEL_FUNCS:
        if re.search(r"\b" + f + r"\b", s, re.IGNORECASE):
            found.append(f)
    return found


# ----------------------------------------------------------------------------
# Measure synonyms
# ----------------------------------------------------------------------------
#
# Every emitted measure carries a `synonyms:` field listing the original PBI
# measure name verbatim. Databricks metric views (DBR 17.2+) accept `synonyms:`
# as a list on each measure; Genie / UC AI/BI consumers use it to resolve user
# queries to the right metric-view measure when the migrated `name:` differs
# from what the user typed.
#
# Comments (the `comment:` field) are NOT generated here. They are LLM-authored
# by the agent calling this skill, who has full understanding of the measure's
# business meaning. The converter preserves any human-authored description from
# the PBI model as a starting point; everything else is the agent's job.
#
# For variations of the original name worth indexing as synonyms (the agent
# may add more — e.g. ASCII-only or de-spaced forms), this helper builds the
# baseline list.

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
      - The bare PBI column name (when it differs from `dim_name`) — what most
        Genie / AI users will type
      - The qualified `'Table'[Column]` form (when it differs from `dim_name`)
        — what someone reading the original PBI report or DAX will type

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
# Top-level entry
# ----------------------------------------------------------------------------

# `_translate_calculate` recurses into `translate_dax_expr_inner`, which is the
# entry point used both by the public wrapper and by recursion.
def translate_dax_expr_inner(dax: str) -> tuple[str, list[str]]:
    s = dax
    s = _translate_calculate(s)
    s = _translate_iferror(s)
    s = _translate_format(s)
    s = _translate_concatenate(s)
    s = _translate_countrows(s)
    s = _translate_divide(s)
    s = _translate_if(s)
    s = _translate_switch(s)
    s, agg_warn = _translate_simple_aggs(s)

    s = _drop_table_qualifier(s)
    s = _convert_string_literals(s)
    # Order matters: translate DAX logical ops (&& → AND, || → OR) BEFORE
    # string concat (& → ||), otherwise the SQL || we just produced gets
    # rewritten back to OR.
    s = _translate_logical_ops(s)
    s = _translate_string_concat(s)
    s = _translate_blank(s)
    s = _translate_truefalse(s)
    return s.strip(), agg_warn


def translate_dax_expr(dax: str) -> tuple[str, list[str], dict]:
    """Translate a DAX expression to a SQL expression usable inside a metric view.

    Returns (sql, warnings, flags). flags includes:
        needs_latest_snapshot: bool — measure references LASTDATE on the calendar
        needs_first_snapshot:  bool — measure references FIRSTDATE
        time_intel:            list[str] — DAX time-intel funcs detected
        needs_manual_review:   bool — translation is incomplete / DAX-only
    """
    global _TRANSLATION_FLAGS
    _TRANSLATION_FLAGS = {}

    warnings: list[str] = []
    flags: dict = {}

    s = dax.strip()
    if s.startswith("="):
        s = s[1:].strip()
    s = strip_dax_comments(s)
    s = expand_power_literals(s)

    # 1) Unwrap UI-edge-case IF(SELECTEDVALUE(...)=..., "-", real) wrappers.
    #    First pass before VAR inlining handles measures whose top-level shape
    #    is already an IF wrapper (no VAR/RETURN around them).
    s = unwrap_slicer_if(s)

    # 2) Inline VAR / RETURN if present (best-effort).
    inlined = inline_var_return(s)
    if inlined is not None:
        s = inlined
        # 2b) Re-run unwrap AFTER inlining — many measures hide the IF chain
        #     inside `VAR Output = IF(...) RETURN Output` form, where the outer
        #     pre-inline shape isn't an IF and so the first pass misses it.
        s = unwrap_slicer_if(s)

    # 3) Atomic translations FIRST — CALCULATE/IF/agg rewrites; this also
    #    consumes DATEADD-d-1 patterns inside CALCULATE filters and rewrites
    #    them to is_yesterday_snapshot, so we don't want to flag those below.
    sql, agg_warn = translate_dax_expr_inner(s)
    warnings.extend(agg_warn)

    # 4) Run unwrap once more on the translated form — DAX `IF(=BLANK()…)`
    #    becomes SQL `IF(<expr> IS NULL, …)` after translation; the unwrap
    #    can keep stripping placeholder branches at this stage too.
    sql = unwrap_slicer_if(sql)

    # 5) Now detect surviving time-intel / filter-context constructs in the
    #    translated SQL. Anything still here was NOT consumed by the inner
    #    translator and genuinely needs manual review.
    time_intel = detect_time_intel(sql)
    if time_intel:
        flags["time_intel"] = time_intel
        for tk in time_intel:
            warnings.append(f"contains {tk} (manual rewrite — see references/dax-to-sql-patterns.md § Period-over-period patterns)")

    # 6) Detect remaining VAR/RETURN (couldn't inline) — flag.
    if re.search(r"\bVAR\b", sql, re.IGNORECASE) or re.search(r"\bRETURN\b", sql, re.IGNORECASE):
        warnings.append("contains VAR/RETURN that could not be inlined automatically")

    # 7) Detect filter-context manipulation.
    for tk in ("ALLEXCEPT", "ALLSELECTED", "USERELATIONSHIP", "EARLIER",
               "EARLIEST", "RANKX", "TOPN", "LOOKUPVALUE"):
        if re.search(r"\b" + tk + r"\b", sql, re.IGNORECASE):
            warnings.append(f"contains {tk} (manual rewrite required)")
    if re.search(r"\bALL\s*\(", sql, re.IGNORECASE):
        warnings.append("contains ALL( (manual rewrite — drops filter context)")

    # 8) Detect leftover SELECTEDVALUE/ISFILTERED that survived all unwraps.
    if _SLICER_FUNCS_RE.search(sql):
        warnings.append("contains SELECTEDVALUE/ISFILTERED (slicer-context — manual rewrite)")

    # 9) Surface the LASTDATE/FIRSTDATE/yesterday-snapshot flags from the side channel.
    if _TRANSLATION_FLAGS.get("needs_latest_snapshot"):
        flags["needs_latest_snapshot"] = True
    if _TRANSLATION_FLAGS.get("needs_first_snapshot"):
        flags["needs_first_snapshot"] = True
    if _TRANSLATION_FLAGS.get("needs_yesterday_snapshot"):
        flags["needs_yesterday_snapshot"] = True

    flags["needs_manual_review"] = bool(warnings)
    return sql, warnings, flags


# ----------------------------------------------------------------------------
# .pbit (Power BI Template) loader
# ----------------------------------------------------------------------------
#
# A .pbit is a ZIP. The relevant member is `DataModelSchema` (UTF-8 JSON, may
# have a leading BOM and may be UTF-16 encoded in some PBI versions).
# Structure (TMSL):
#   { "name": "...", "compatibilityLevel": ..., "model": { ... } }
# Within `model`:
#   tables: [{name, columns:[{name, dataType, isHidden, summarizeBy, expression?}],
#             measures:[{name, expression, description?}]}]
#   relationships: [{name, fromTable, fromColumn, toTable, toColumn, isActive}]
#

_PBIT_SCHEMA_MEMBER_CANDIDATES = ("DataModelSchema", "DataModel/Schema")


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
    # Try common encodings — PBI usually writes UTF-16 LE BOM, sometimes UTF-8.
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
    """Open a .pbit and return a normalized model dict shaped like the older
    .pbix loader (so the rest of the pipeline is unchanged):
        {tables: [{name, columns:[...], measures:[...]}], relationships: [...]}
    """
    with zipfile.ZipFile(path) as zf:
        root = _read_datamodel_schema(zf)

    model = root.get("model") or {}
    tables_in = model.get("tables") or []

    # Build tables list
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
            })
        tables_out.append({
            "name": name,
            "columns": cols_out,
            "measures": measures_out,
            "isHidden": bool(t.get("isHidden", False)),
            "isCalcTable": _is_calc_table(t),
        })

    # Relationships
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


# Hidden, auto-generated PBI tables.
_AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")


def _is_real_table(name: str) -> bool:
    return not name.startswith(_AUTO_DATE_PREFIXES)


# ----------------------------------------------------------------------------
# Fact picking + metric view assembly
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
    Returns the (original) column name, or None."""
    fact_name = fact["name"]
    # Heuristic 1: an active relationship from fact to a table whose name
    # contains 'calendar' or 'date'.
    for r in doc.get("relationships", []):
        if r.get("fromTable") != fact_name or r.get("isActive") is False:
            continue
        to = (r.get("toTable") or "").lower()
        if "calendar" in to or "date" in to:
            return r["fromColumn"]
    # Heuristic 2: a fact column whose name contains 'date' or 'snapshot'
    # and whose data type is dateTime/date.
    for c in fact.get("columns", []):
        n = c["name"].lower()
        dt = (c.get("dataType") or "").lower()
        if ("date" in n or "snapshot" in n) and ("date" in dt or "time" in dt):
            return c["name"]
    # Heuristic 3: any datetime column.
    for c in fact.get("columns", []):
        dt = (c.get("dataType") or "").lower()
        if "date" in dt or "time" in dt:
            return c["name"]
    return None


def topo_sort_measures(measures: list[dict]) -> list[dict]:
    """Sort measures so each measure's MEASURE(`X`) refs are to measures defined earlier.
    Falls back to original order on cycles (with a warning attached to the offending measure).
    """
    name_to_idx = {m["name"]: i for i, m in enumerate(measures)}
    # edges: from m → measures it references via MEASURE(`...`)
    edges: dict[int, set[int]] = {i: set() for i in range(len(measures))}
    ref_re = re.compile(r"MEASURE\s*\(\s*`([^`]+)`\s*\)", re.IGNORECASE)
    for i, m in enumerate(measures):
        for r in ref_re.finditer(m.get("expr", "")):
            other = r.group(1)
            j = name_to_idx.get(other)
            if j is not None and j != i:
                edges[i].add(j)
    # Kahn's algorithm: a measure can be emitted once all its refs are emitted.
    emitted: list[int] = []
    seen: set[int] = set()

    def visit(i: int, stack: set[int]) -> None:
        if i in seen:
            return
        if i in stack:
            # cycle — bail; emit anyway in the original order
            return
        stack.add(i)
        for j in edges[i]:
            visit(j, stack)
        stack.discard(i)
        if i not in seen:
            seen.add(i)
            emitted.append(i)
    for i in range(len(measures)):
        visit(i, set())
    return [measures[i] for i in emitted]


def build_metric_view(
    doc: dict,
    source_override: str | None,
    fact_override: str | None,
    style: str = "kimball",
    catalog_schema: str | None = None,
    fact_suffix: str | None = None,
) -> tuple[dict, list[str]]:
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

    col_rename: dict[str, str] = {}
    for t in tables:
        for c in t.get("columns", []):
            orig = c["name"]
            phys = physical_col(orig)
            col_rename[orig] = phys

    def rewrite_col_refs(sql: str) -> str:
        if style != "kimball":
            return sql
        # Sort by length desc so longer names match first (avoid partial-replace issues).
        for orig in sorted(col_rename.keys(), key=len, reverse=True):
            phys = col_rename[orig]
            if orig == phys:
                continue
            sql = sql.replace(f"`{orig}`", f"`{phys}`")
        return sql

    # Joins (only direct fact -> dim, active)
    join_aliases: dict[str, str] = {}
    joins: list[dict] = []
    for r in relationships:
        if r.get("fromTable") != fact_name:
            continue
        if r.get("isActive") is False:
            warnings.append(f"inactive relationship to {r['toTable']!r} skipped — wire manually if needed (USERELATIONSHIP equivalent)")
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

    # Dimensions: non-numeric / non-summarized fact columns + all join-table columns.
    # Each entry carries:
    #   - name:     the metric-view dimension name
    #   - expr:     the resolved SQL expression
    #   - synonyms: bare PBI col name (if differs) + 'Table'[Column] DAX form (if differs)
    #   - comment:  preserved-from-PBI description if present (otherwise omitted —
    #               the agent calling this skill is expected to LLM-author the rest)
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
            pbi_desc=_expr_to_string(col.get("description")) if col.get("description") else None,
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
                pbi_desc=_expr_to_string(col.get("description")) if col.get("description") else None,
            ))

    if not dimensions:
        warnings.append("no dimensions discovered; metric view requires at least one")

    # Measures: translate each
    measures: list[dict] = []
    needs_latest = False
    needs_first = False
    needs_yesterday = False
    for t in tables:
        for m in t.get("measures", []) or []:
            dax = m.get("expression", "") or ""
            sql, warn, flags = translate_dax_expr(dax)
            entry = {
                "name": m["name"],
                "expr": sql,
                "_dax": dax,
                "_warn": warn,
                "_flags": flags,
            }
            # Synonyms: always include the bracketed DAX form `[<orig>]` so a
            # user typing the original DAX reference finds the migrated measure.
            # The bare original name is included only if a hand-edit later renames
            # the metric-view `name:` (build_measure_synonyms drops it when name
            # matches, so default no-rename emits only `[<orig>]`).
            entry["synonyms"] = build_measure_synonyms(m["name"], m["name"])
            # Comment: only the human-authored PBI description is preserved.
            # The agent calling this skill is expected to LLM-author richer
            # comments (one per measure) based on the DAX expression, business
            # context, and naming. The converter does NOT auto-generate comments
            # — rule-based shape detection produces shallow, generic text that
            # often misleads ("Ratio of x to expr") and is worse than no comment.
            desc = _expr_to_string(m.get("description")) if m.get("description") else None
            if desc:
                entry["comment"] = desc.strip()
            measures.append(entry)
            if warn:
                warnings.append(f"measure {m['name']!r}: " + "; ".join(warn))
            needs_latest = needs_latest or flags.get("needs_latest_snapshot", False)
            needs_first = needs_first or flags.get("needs_first_snapshot", False)
            needs_yesterday = needs_yesterday or flags.get("needs_yesterday_snapshot", False)

    # Resolve bare-bracket [Measure] references
    measure_name_set = {m["name"] for m in measures}

    def resolve_bare_brackets(sql: str) -> str:
        def rep(mt):
            x = mt.group(1)
            if x in measure_name_set:
                return f"MEASURE(`{x}`)"
            return f"`{x}`"
        return re.sub(r"\[([^\]]+)\]", rep, sql)

    for m in measures:
        m["expr"] = rewrite_col_refs(resolve_bare_brackets(m["expr"]))

    # Topo-sort to make all forward MEASURE() refs backward
    measures = topo_sort_measures(measures)

    # Detect remaining forward refs (after sort) — those are cycles, inline the
    # referenced measure's expr if simple, else flag.
    pos = {m["name"]: i for i, m in enumerate(measures)}
    fwd_refs_inlined = 0
    ref_re = re.compile(r"MEASURE\s*\(\s*`([^`]+)`\s*\)", re.IGNORECASE)
    for i, m in enumerate(measures):
        def rep(mt, i=i):
            nonlocal fwd_refs_inlined
            target = mt.group(1)
            j = pos.get(target)
            if j is not None and j > i:
                tgt = measures[j]
                # Only inline simple, non-flagged exprs to avoid runaway expansion
                if not tgt.get("_flags", {}).get("needs_manual_review", False):
                    fwd_refs_inlined += 1
                    return "(" + tgt["expr"] + ")"
            return mt.group(0)
        m["expr"] = ref_re.sub(rep, m["expr"])

    if not measures:
        warnings.append("no measures discovered; metric view requires at least one")

    # Source: SQL block when augmentation is needed; else plain table ref.
    needs_any_snapshot = needs_latest or needs_first or needs_yesterday
    fact_date_col_orig = detect_fact_date_column(fact, doc) if needs_any_snapshot else None

    if needs_any_snapshot:
        if fact_date_col_orig is None:
            warnings.append(
                "LASTDATE/FIRSTDATE/DATEADD-D-1 detected but no date column found on fact; "
                "is_latest_snapshot/is_first_snapshot/is_yesterday_snapshot flags emitted "
                "against `data_cutoff_dt` as a default — adjust if your fact uses a different date column."
            )
            fact_date_col_phys = "data_cutoff_dt"
        else:
            fact_date_col_phys = physical_col(fact_date_col_orig)
        source_lines = [
            f"  SELECT",
            f"    f.*,",
        ]
        if needs_latest:
            source_lines.append(
                f"    (`{fact_date_col_phys}` = MAX(`{fact_date_col_phys}`) OVER ()) AS is_latest_snapshot,"
            )
        if needs_first:
            source_lines.append(
                f"    (`{fact_date_col_phys}` = MIN(`{fact_date_col_phys}`) OVER ()) AS is_first_snapshot,"
            )
        if needs_yesterday:
            # Use DENSE_RANK over distinct dates so this works even when the
            # fact has gaps (weekends, holidays, etc.) — "yesterday" is the
            # second-most-recent snapshot date, not literally MAX-1day.
            source_lines.append(
                f"    (DENSE_RANK() OVER (ORDER BY `{fact_date_col_phys}` DESC) = 2) "
                f"AS is_yesterday_snapshot,"
            )
        # remove trailing comma on last addition
        source_lines[-1] = source_lines[-1].rstrip(",")
        source_lines.append(f"  FROM {fq_table(fact_name)} f")
        source_value = "|\n" + "\n".join(source_lines)
    else:
        source_value = fq_table(fact_name)

    mv: dict = {
        "version": "1.1",
        "comment": (f"Generated from Power BI .pbit tabular model. "
                    f"Fact table: {physical_table(fact_name)} ({style} style)."),
        "source": source_override or source_value,
    }
    if joins:
        mv["joins"] = joins
    mv["dimensions"] = dimensions
    mv["measures"] = measures

    if fwd_refs_inlined:
        warnings.append(f"forward measure references: {fwd_refs_inlined} inlined to satisfy backward-only resolution")

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
        # Dedupe column names within the table (Spark forbids dupes)
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
        ddl = (
            f"CREATE OR REPLACE TABLE {cs_prefix}.{physical} (\n"
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
# YAML emission
# ----------------------------------------------------------------------------

def _yaml_scalar(v: str) -> str:
    if v == "":
        return '""'
    needs_quote = any(c in v for c in ":#&*!|>'\"%@`{},[]") or v.strip() != v
    if needs_quote or "\n" in v:
        return json.dumps(v, ensure_ascii=False)
    return v


def emit_yaml(mv: dict) -> str:
    out: list[str] = []
    out.append(f"version: \"{mv['version']}\"")
    if mv.get("comment"):
        out.append(f"comment: {_yaml_scalar(mv['comment'])}")
    src = mv["source"]
    if src.startswith("|"):
        # multi-line literal block
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
        out.append(f"  - name: {_yaml_scalar(d['name'])}")
        out.append(f"    expr: {_yaml_scalar(d['expr'])}")
        if d.get("comment"):
            out.append(f"    comment: {_yaml_scalar(d['comment'])}")
        else:
            out.append(f"    # comment: TODO — agent should LLM-author a one-line description")
            out.append(f"    #          (what this dimension represents in business terms).")
        d_syns = [s for s in (d.get("synonyms") or []) if s and s != d["name"]]
        if d_syns:
            out.append(f"    synonyms:")
            for syn in d_syns:
                out.append(f"      - {_yaml_scalar(syn)}")
    out.append("measures:")
    for m in mv["measures"]:
        if m.get("_warn"):
            out.append(f"  # TODO manual review — original DAX:")
            for line in (m.get("_dax") or "").splitlines() or [""]:
                out.append(f"  #   {line}")
            out.append(f"  # warnings: {'; '.join(m['_warn'])}")
            # Direct future-Claude / human editor to the right hand-fix shape.
            warns_joined = " ".join(m["_warn"])
            if any(kw in warns_joined for kw in ("TOTALYTD", "SAMEPERIODLASTYEAR", "DATEADD", "PARALLELPERIOD", "PREVIOUSDAY", "PREVIOUSMONTH", "PREVIOUSQUARTER", "PREVIOUSYEAR", "DATESYTD")):
                out.append(f"  # HOW-TO: rewrite this as a `window:` block. The `expr:` MUST reference")
                out.append(f"  #   the BASE measure via MEASURE(), e.g.  expr: \"MEASURE(`Total COGS`)\"")
                out.append(f"  #   — DO NOT re-inline `SUM(...)+SUM(...)` here (breaks single-source-of-truth).")
                out.append(f"  #   For year-trailing windows add a DATE-typed dim like `DATE_TRUNC('YEAR', date.\\`date\\`)`.")
                out.append(f"  #   See SKILL.md non-negotiable default #3 and references/dax-to-sql-patterns.md § PoP patterns.")
        out.append(f"  - name: {_yaml_scalar(m['name'])}")
        out.append(f"    expr: {_yaml_scalar(m['expr'])}")
        if m.get("comment"):
            out.append(f"    comment: {_yaml_scalar(m['comment'])}")
        else:
            out.append(f"    # comment: TODO — agent should LLM-author a one-line description")
            out.append(f"    #          based on the DAX expression and business context.")
        syns = [s for s in (m.get("synonyms") or []) if s and s != m["name"]]
        if syns:
            out.append(f"    synonyms:")
            for syn in syns:
                out.append(f"      - {_yaml_scalar(syn)}")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Verification (levels 3, 5, 6)
# ----------------------------------------------------------------------------

_BACKTICK_REF = re.compile(r"(?:(\w+)\.)?`([^`]+)`")
_MEASURE_REF = re.compile(r"\bMEASURE\s*\(\s*`([^`]+)`\s*\)", re.IGNORECASE)


def verify_static(doc: dict, mv: dict, style: str, fact_orig: str) -> list[str]:
    issues: list[str] = []

    def physical(name: str) -> str:
        return kimball_col(name) if style == "kimball" else name

    fact = next((t for t in doc.get("tables", []) if t["name"] == fact_orig), None)
    fact_cols: set[str] = set()
    if fact:
        fact_cols = {physical(c["name"]) for c in fact.get("columns", [])}
        # Augmented columns from SQL source (when LASTDATE/FIRSTDATE detected)
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

    measure_names: set[str] = {m["name"] for m in mv.get("measures", [])}

    def check_expr(expr: str, label: str) -> None:
        for mt in _MEASURE_REF.finditer(expr):
            x = mt.group(1)
            if x not in measure_names:
                issues.append(f"{label}: MEASURE(`{x}`) — no such declared measure")
        work = _MEASURE_REF.sub("", expr)
        for ub in re.finditer(r"\[([^\]]+)\]", work):
            issues.append(f"{label}: unresolved bare bracket [{ub.group(1)}] — manual rewrite needed")
        for mt in _BACKTICK_REF.finditer(work):
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
    for m in mv.get("measures", []):
        check_expr(m.get("expr", ""), f"measure {m['name']!r}")
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


def emit_verify_sql(mv: dict, catalog_schema: str | None, view_full_name: str) -> str:
    lines: list[str] = []
    lines.append("-- ============================================================")
    lines.append("-- VERIFICATION: compile-check each measure against the view.")
    lines.append("-- Run after the CREATE TABLE / CREATE VIEW above succeed.")
    lines.append("-- ============================================================")
    lines.append("")
    lines.append(f"DESCRIBE EXTENDED {view_full_name};")
    lines.append("")
    for m in mv.get("measures", []):
        safe_name = m["name"].replace("`", "``")
        lines.append(f"-- {m['name']}")
        lines.append(f"SELECT MEASURE(`{safe_name}`) AS m FROM {view_full_name} LIMIT 1;")
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert a Power BI .pbit into Databricks UC metric view YAML (+ optional DDL).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (kimball default — Genie/dbt idiomatic):\n"
            "  dax_to_metric_view.py model.pbit --catalog-schema main.sales --emit-ddl --out sales.sql\n"
            "  dax_to_metric_view.py model.pbit --catalog-schema main.sales --fact-suffix profitability\n"
            "\n"
            "Source-fidelity (preserve PBI names verbatim — needs Delta column mapping):\n"
            "  dax_to_metric_view.py model.pbit --style fidelity --source main.sales.fact_sales\n"
            "\n"
            "Schema-only output by default. Data ingestion is a separate step.\n"
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
                    help="Run static schema check (level 3) + structural diff (level 6).")
    ap.add_argument("--emit-verify-sql", action="store_true",
                    help="Append a verification SQL block (level 5) — one SELECT MEASURE() per measure.")
    ap.add_argument("--strict", action="store_true",
                    help="Exit non-zero if --verify found issues OR any measure was flagged.")
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

    yaml_body = emit_yaml(mv)
    cs = args.catalog_schema if args.catalog_schema else "<TODO_CATALOG.SCHEMA>"
    fact_phys = (kimball_table(fact_orig, is_fact=True, fact_suffix=args.fact_suffix)
                 if args.style == "kimball" else fact_orig)
    metric_view_name = f"{cs}.{fact_phys}_metrics"
    parts.append(
        f"CREATE OR REPLACE VIEW {metric_view_name}\n"
        f"WITH METRICS\n"
        f"LANGUAGE YAML\n"
        f"AS $$\n{yaml_body}$$;\n"
    )

    if args.emit_verify_sql:
        parts.append(emit_verify_sql(mv, args.catalog_schema, metric_view_name))

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
              f"{len(mv.get('measures', []))} measures, "
              f"{len(mv.get('joins', []))} joins", file=sys.stderr)
        print(f"  Level 3 (static schema check): {len(static_issues)} issue(s)", file=sys.stderr)
        for i in static_issues[:20]:
            print(f"    • {i}", file=sys.stderr)
        if len(static_issues) > 20:
            print(f"    ... +{len(static_issues) - 20} more", file=sys.stderr)
        print(f"  Level 6 (structural diff): {len(structural_issues)} issue(s)", file=sys.stderr)
        for i in structural_issues:
            print(f"    • {i}", file=sys.stderr)
        if args.emit_verify_sql:
            print(f"  Level 5 (live compile): verify SQL appended to output — run via execute_sql.",
                  file=sys.stderr)

    if warnings:
        print("\n=== Diagnostic (translation flags) ===", file=sys.stderr)
        for w in warnings:
            print(f"  • {w}", file=sys.stderr)
        print(f"  ({len(warnings)} item(s) need manual review)", file=sys.stderr)

    if args.strict and (warnings or static_issues or structural_issues):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
