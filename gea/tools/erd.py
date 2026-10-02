"""Draw the entity-relationship diagram of the `gea` schema from the live catalogue.

    python gea/tools/erd.py --database-url postgresql://gea_owner:secret@localhost:5432/gea
    python gea/tools/erd.py --database-url ... --output gea/db/postgres/erd.svg

The picture is read from pg_catalog, so it cannot drift from the migrations:
run it after `alembic upgrade head` and commit the result. Needs `psql` and
Graphviz `dot` on PATH; only Python's standard library otherwise.

--css-vars writes colours as CSS custom properties (var(--erd-...)) instead of
fixed values, for embedding the SVG inline in a page that has a dark theme.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Table -> domain. A table missing here is still drawn, under "Other".
GROUPS: dict[str, list[str]] = {
    "Users and lookups": ["User", "Role", "Region", "BusinessPurpose", "Benefit"],
    "Project and run (workbook sheet POC Data)": ["Project", "ProjectBenefit", "Run", "RunStudyPeriodExclusion"],
    "Jobs": ["Job"],
    "Dropdown values": ["ParameterOption"],
    "Data contract, delivery and execution": ["DataContract", "ContractDelivery", "RunExecution", "RunExecutionStep", "RunLog"],
    "Cross-cutting": ["CommandReceipt", "AuditEvent", "AlembicVersion"],
}

# Foreign keys that are written next to the column ("-> User") instead of being
# drawn. Every table points at "User" for CreatedBy / UpdatedBy and the like, and
# "ParameterOption" points at the three lookups as optional scope filters; as lines
# they cross the whole picture and hide the model.
INLINE_TARGETS = {"User"}
INLINE_KEYS = {("ParameterOption", "Region"), ("ParameterOption", "BusinessPurpose"), ("ParameterOption", "Benefit")}
# A table without any drawn relation is laid out in the column right of this one,
# so that the picture stays compact instead of growing a column of loose tables.
ANCHOR = "User"


def is_inline(source: str, target: str) -> bool:
    return source != target and (target in INLINE_TARGETS or (source, target) in INLINE_KEYS)

# Placeholder colours: unique values that are replaced after layout, either by
# the light palette or by CSS custom properties.
PALETTE = {
    "#010101": ("ink", "#172B46"),
    "#020202": ("muted", "#5B6B82"),
    "#030303": ("line", "#8A97AB"),
    "#040404": ("surface", "#FFFFFF"),
    "#050505": ("head", "#E6EEFC"),
    "#060606": ("accent", "#356CC9"),
    "#070707": ("group", "#F5F7FA"),
    "#080808": ("key", "#A96915"),
}
INK, MUTED, LINE, SURFACE, HEAD, ACCENT, GROUP, KEY = PALETTE

CATALOGUE_SQL = r"""
SELECT json_build_object(
  'tables', (
    SELECT json_agg(json_build_object(
        'name', c.relname,
        'columns', (
            SELECT json_agg(json_build_object(
                'name', a.attname,
                'type', format_type(a.atttypid, a.atttypmod),
                'notNull', a.attnotnull,
                'generated', a.attgenerated <> '' OR a.attidentity <> ''
            ) ORDER BY a.attnum)
            FROM pg_attribute a
            WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
        'primaryKey', coalesce((
            SELECT json_agg(a.attname ORDER BY k.ord)
            FROM pg_constraint p
            CROSS JOIN LATERAL unnest(p.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = p.conrelid AND a.attnum = k.attnum
            WHERE p.conrelid = c.oid AND p.contype = 'p'), '[]'::json),
        'unique', coalesce((
            SELECT json_agg(cols) FROM (
                SELECT (SELECT json_agg(a.attname ORDER BY a.attname)
                        FROM unnest(u.conkey) AS k(attnum)
                        JOIN pg_attribute a ON a.attrelid = u.conrelid AND a.attnum = k.attnum) AS cols
                FROM pg_constraint u
                WHERE u.conrelid = c.oid AND u.contype IN ('p', 'u')) s), '[]'::json)
    ) ORDER BY c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :'schema' AND c.relkind = 'r'),
  'foreignKeys', coalesce((
    SELECT json_agg(json_build_object(
        'name', f.conname,
        'from', src.relname,
        'to', dst.relname,
        'columns', (SELECT json_agg(a.attname ORDER BY k.ord)
                    FROM unnest(f.conkey) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute a ON a.attrelid = f.conrelid AND a.attnum = k.attnum),
        'references', (SELECT json_agg(a.attname ORDER BY k.ord)
                       FROM unnest(f.confkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_attribute a ON a.attrelid = f.confrelid AND a.attnum = k.attnum),
        'deferred', f.condeferred
    ) ORDER BY src.relname, f.conname)
    FROM pg_constraint f
    JOIN pg_class src ON src.oid = f.conrelid
    JOIN pg_class dst ON dst.oid = f.confrelid
    JOIN pg_namespace n ON n.oid = src.relnamespace
    WHERE f.contype = 'f' AND n.nspname = :'schema'), '[]'::json)
)::text;
"""

SHORT_TYPES = {
    "timestamp with time zone": "timestamptz",
    "character varying": "varchar",
    "character(64)": "char(64)",
    "boolean": "bool",
    "integer": "int",
    "smallint": "smallint",
}


def read_catalogue(database_url: str, schema: str) -> dict:
    result = subprocess.run(
        ["psql", database_url, "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"schema={schema}", "-f", "-"],
        input=CATALOGUE_SQL, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"psql failed:\n{result.stderr}")
    catalogue = json.loads(result.stdout)
    if not catalogue["tables"]:
        sys.exit(f"Schema {schema!r} has no tables. Run the migrations first.")
    return catalogue


def table_node(table: dict, foreign: dict[str, str]) -> str:
    """One table as a Graphviz HTML-like label. Each column row is a port."""
    primary = set(table["primaryKey"])
    rows = [f'<TR><TD COLSPAN="3" BGCOLOR="{HEAD}" ALIGN="LEFT" CELLPADDING="6">'
            f'<FONT COLOR="{INK}" POINT-SIZE="13"><B>{html.escape(table["name"])}</B></FONT></TD></TR>']
    for column in table["columns"]:
        name = column["name"]
        marks = [m for m, on in (("PK", name in primary), ("FK", name in foreign)) if on]
        kind = SHORT_TYPES.get(column["type"], column["type"])
        if not column["notNull"]:
            kind += " ?"
        if name in foreign and is_inline(table["name"], foreign[name]):
            kind += f"  → {foreign[name]}"
        label = f"<B>{html.escape(name)}</B>" if name in primary else html.escape(name)
        rows.append(
            f'<TR><TD PORT="{name}_w" ALIGN="LEFT" WIDTH="26"><FONT COLOR="{KEY}" POINT-SIZE="9">'
            f'{" ".join(marks) or " "}</FONT></TD>'
            f'<TD ALIGN="LEFT"><FONT COLOR="{INK}">{label}</FONT></TD>'
            f'<TD PORT="{name}_e" ALIGN="LEFT"><FONT COLOR="{MUTED}">{html.escape(kind)}</FONT></TD></TR>')
    return (f'"{table["name"]}" [label=<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" '
            f'COLOR="{LINE}" BGCOLOR="{SURFACE}">{"".join(rows)}</TABLE>>];')


def build_dot(catalogue: dict) -> str:
    tables = {table["name"]: table for table in catalogue["tables"]}
    foreign: dict[str, dict[str, str]] = {name: {} for name in tables}
    for key in catalogue["foreignKeys"]:
        for column in key["columns"]:
            foreign[key["from"]].setdefault(column, key["to"])

    lines = [
        "digraph gea {",
        '  graph [rankdir=LR, splines=spline, nodesep=0.35, ranksep=1.1, pad=0.3, bgcolor="transparent",'
        ' fontname="Helvetica", fontsize=12, newrank=true, compound=true];',
        '  node [shape=plain, fontname="Helvetica", fontsize=10.5];',
        f'  edge [color="{ACCENT}", penwidth=1.2, arrowsize=0.8, dir=both, fontname="Helvetica",'
        f' fontsize=9, fontcolor="{MUTED}"];',
    ]
    placed: set[str] = set()
    groups = dict(GROUPS)
    groups["Other"] = [name for name in tables if not any(name in members for members in GROUPS.values())]
    for index, (title, members) in enumerate(groups.items()):
        present = [name for name in members if name in tables]
        if not present:
            continue
        lines.append(f'  subgraph cluster_{index} {{')
        lines.append(f'    label="{title}"; labeljust="l"; style="rounded,filled"; color="{LINE}"; '
                     f'fillcolor="{GROUP}"; fontcolor="{MUTED}"; margin=14;')
        for name in present:
            lines.append("    " + table_node(tables[name], foreign[name]))
            placed.add(name)
        lines.append("  }")

    for key in catalogue["foreignKeys"]:
        if is_inline(key["from"], key["to"]):
            continue                                   # written next to the column instead
        child, parent = tables[key["from"]], tables[key["to"]]
        optional = any(not column["notNull"] for column in child["columns"] if column["name"] in key["columns"])
        one_to_one = sorted(key["columns"]) in [sorted(columns) for columns in child["unique"]]
        child_end = "teeodot" if one_to_one else "crowodot"
        parent_end = "teeodot" if optional else "teetee"
        # The edge runs parent -> child so that parents are laid out to the left;
        # the arrow shapes still say "many children, one parent".
        if key["from"] == key["to"]:
            tail, head = f'{key["references"][0]}_e:e', f'{key["columns"][0]}_e:e'
        elif key["deferred"]:
            # A deferred key closes a cycle: its parent is laid out to the right of the child.
            tail, head = f'{key["references"][0]}_w:w', f'{key["columns"][-1]}_e:e'
        else:
            tail, head = f'{key["references"][0]}_e:e', f'{key["columns"][0]}_w:w'
        attributes = [f"arrowtail={parent_end}", f"arrowhead={child_end}"]
        if key["deferred"]:
            attributes.append("style=dashed")
        if key["deferred"] or key["from"] == key["to"]:
            attributes.append("constraint=false")
        if len(key["columns"]) > 1:
            attributes.append(f'label="({", ".join(key["columns"])})"')
        lines.append(f'  "{key["to"]}":{tail} -> "{key["from"]}":{head} [{", ".join(attributes)}];')
    drawn = {name for key in catalogue["foreignKeys"] if not is_inline(key["from"], key["to"])
             for name in (key["from"], key["to"])}
    if ANCHOR in tables:
        for name in tables:
            if name not in drawn and name != ANCHOR:
                lines.append(f'  "{ANCHOR}" -> "{name}" [style=invis];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def recolour(svg: str, css_vars: bool) -> str:
    """Swap the placeholder colours for the light palette or for CSS custom properties."""
    def swap(match: re.Match) -> str:
        attribute, colour = match.group(1), match.group(2).lower()
        name, light = PALETTE[colour]
        if css_vars:
            return f'style="{attribute}:var(--erd-{name})"'
        return f'{attribute}="{light}"'

    pattern = re.compile(r'\b(fill|stroke)="(#0[1-8]0[1-8]0[1-8])"', re.IGNORECASE)
    # An element can carry both attributes; merge the two style fragments.
    svg = pattern.sub(swap, svg)
    svg = re.sub(r'style="([^"]*)"((?:\s+[\w:-]+="[^"]*")*?)\s+style="([^"]*)"', r'style="\1;\3"\2', svg)
    svg = re.sub(r"<\?xml[^>]*\?>\s*|<!DOCTYPE[^>]*>\s*|<!--.*?-->\s*", "", svg, flags=re.DOTALL)
    return svg


def stable_order(svg: str) -> str:
    """Write the group frames in the order of their ids.

    Two builds of the same Graphviz version emit them in different orders, so the checked-in
    file would depend on the machine it was drawn on. The frames do not overlap; the order
    in which they are painted changes nothing.
    """
    frames = re.findall(r'<g id="clust\d+" class="cluster">.*?</g>\n', svg, flags=re.S)
    if len(frames) < 2:
        return svg
    start = svg.index(frames[0])
    if svg[start:start + sum(map(len, frames))] != "".join(frames):
        return svg                                     # not one block: leave the file as Graphviz wrote it
    frames.sort(key=lambda frame: int(re.match(r'<g id="clust(\d+)"', frame).group(1)))
    return svg[:start] + "".join(frames) + svg[start + sum(map(len, frames)):]


def render(dot: str, css_vars: bool) -> str:
    result = subprocess.run(["dot", "-Tsvg"], input=dot, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"dot failed:\n{result.stderr}")
    return recolour(stable_order(result.stdout), css_vars)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--schema", default="gea")
    parser.add_argument("--output", type=Path, default=ROOT / "db" / "postgres" / "erd.svg")
    parser.add_argument("--css-vars", action="store_true", help="colours as var(--erd-*) for inline embedding")
    parser.add_argument("--dot", action="store_true", help="write the Graphviz source instead of the SVG")
    args = parser.parse_args()

    catalogue = read_catalogue(args.database_url, args.schema)
    dot = build_dot(catalogue)
    args.output.write_text(dot if args.dot else render(dot, args.css_vars), encoding="utf-8", newline="\n")
    inline = sum(1 for key in catalogue["foreignKeys"] if is_inline(key["from"], key["to"]))
    print(f"{args.output}: {len(catalogue['tables'])} tables, {len(catalogue['foreignKeys'])} foreign keys "
          f"({len(catalogue['foreignKeys']) - inline} drawn as lines, {inline} written next to the column)")


if __name__ == "__main__":
    main()
