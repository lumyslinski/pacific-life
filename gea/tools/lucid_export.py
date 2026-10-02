"""Export the database schema in the format Lucidchart imports as an entity-relationship diagram.

    python gea/tools/lucid_export.py --database-url postgresql://gea_owner:secret@localhost:5432/gea
    python gea/tools/lucid_export.py --database-url ... --check      # CI: the committed file is current

Writes gea/db/postgres/lucid-erd-import.csv. In Lucidchart: Entity Relationship shape library,
Import, PostgreSQL, Next (skip the query: this file is its result), Choose File.

The file is the result of the query Lucidchart itself gives for PostgreSQL, one row per column
and per key the column belongs to, in Lucidchart's twelve columns and without a header row:

    dbms, database, schema, table, column, position, type, length,
    key type (PRIMARY KEY | FOREIGN KEY | UNIQUE | empty), referenced schema, table, column

It differs from Lucidchart's query in five places, none of which changes the shape of a row:
  * only the schemas given with --schema (default: gea and calc) instead of every schema;
  * the database name is a fixed word (--database-name), so the file does not depend on the
    database it was read from;
  * an array column says `text[]` where information_schema says `ARRAY`;
  * the columns of a table are numbered 1, 2, 3 ...: the catalogue keeps a gap where a column was
    dropped, so its numbers depend on the path the migrations took (a downgrade and an upgrade);
  * the rows are ordered, so the file is the same on every run.

Run it as the owner of the schema: information_schema shows the keys of a table only to its
owner or to a role with a privilege on it other than SELECT, and without the keys Lucidchart
draws no relationships. Needs `psql` on PATH; only Python's standard library otherwise.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "db" / "postgres" / "lucid-erd-import.csv"

# The FROM and JOIN clauses are Lucidchart's PostgreSQL import query, unchanged.
QUERY = r"""
COPY (
    SELECT 'postgresql' AS dbms, :'database' AS table_catalog, t.table_schema, t.table_name, c.column_name,
           dense_rank() OVER (PARTITION BY t.table_schema, t.table_name ORDER BY c.ordinal_position) AS ordinal_position,
           CASE WHEN c.data_type = 'ARRAY' THEN substr(c.udt_name, 2) || '[]'
                WHEN c.data_type = 'USER-DEFINED' THEN c.udt_name
                ELSE c.data_type END AS data_type,
           c.character_maximum_length, n.constraint_type,
           k2.table_schema AS referenced_schema, k2.table_name AS referenced_table, k2.column_name AS referenced_column
    FROM information_schema.tables t
    NATURAL LEFT JOIN information_schema.columns c
    LEFT JOIN (information_schema.key_column_usage k
               NATURAL JOIN information_schema.table_constraints n
               NATURAL LEFT JOIN information_schema.referential_constraints r)
           ON c.table_catalog = k.table_catalog AND c.table_schema = k.table_schema
          AND c.table_name = k.table_name AND c.column_name = k.column_name
    LEFT JOIN information_schema.key_column_usage k2
           ON k.position_in_unique_constraint = k2.ordinal_position
          AND r.unique_constraint_catalog = k2.constraint_catalog
          AND r.unique_constraint_schema = k2.constraint_schema
          AND r.unique_constraint_name = k2.constraint_name
    WHERE t.table_type = 'BASE TABLE'
      AND t.table_schema = ANY (string_to_array(:'schemas', ','))
    ORDER BY array_position(string_to_array(:'schemas', ','), t.table_schema::text),
             t.table_name COLLATE "C", c.ordinal_position,
             CASE n.constraint_type WHEN 'PRIMARY KEY' THEN 1 WHEN 'FOREIGN KEY' THEN 2 ELSE 3 END,
             n.constraint_name COLLATE "C", k.ordinal_position
) TO STDOUT WITH (FORMAT csv);
"""


# How many key columns the schemas really have. pg_catalog is readable by every role, so this
# number does not depend on who asks.
KEY_COLUMNS = r"""
SELECT count(*)
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(k.conkey) AS u(attnum)
WHERE n.nspname = ANY (string_to_array(:'schemas', ',')) AND k.contype IN ('p', 'f', 'u');
"""


def psql(database_url: str, script: str, schemas: list[str], database_name: str) -> str:
    result = subprocess.run(
        ["psql", database_url, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-v", f"schemas={','.join(schemas)}",
         "-v", f"database={database_name}", "-f", "-"],
        input=script, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"psql failed:\n{result.stderr}")
    return result.stdout


def export(database_url: str, schemas: list[str], database_name: str) -> str:
    rows = psql(database_url, QUERY, schemas, database_name).splitlines()
    if not rows:
        sys.exit(f"No table found in schema {', '.join(schemas)}. Run the migrations first.")
    keys = sum(1 for row in csv.reader(rows) if row[8])
    expected = int(psql(database_url, KEY_COLUMNS, schemas, database_name))
    if keys != expected:
        sys.exit(f"The export shows {keys} of the {expected} key columns of the schema: this login does not own the tables, "
                 "and information_schema hides their keys from it. Run the export as the owner of the schema; "
                 "without the keys Lucidchart draws no relationships.")
    return "\n".join(rows) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--schema", action="append", help="repeatable; default: gea and calc")
    parser.add_argument("--database-name", default="gea", help="the database name written into the file")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="write nothing; fail when the file is not current")
    args = parser.parse_args()

    text = export(args.database_url, args.schema or ["gea", "calc"], args.database_name)
    rows = list(csv.reader(text.splitlines()))
    summary = (f"{len({(row[2], row[3]) for row in rows})} tables, {len(rows)} rows, "
               f"{sum(row[8] == 'FOREIGN KEY' for row in rows)} foreign-key columns")
    if args.check:
        current = args.output.read_bytes().replace(b"\r\n", b"\n").decode("utf-8") if args.output.is_file() else None
        if current != text:
            sys.exit(f"{args.output} is not the schema of this database. Run gea/tools/lucid_export.py and commit the result.")
        print(f"{args.output}: up to date ({summary})")
        return
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"{args.output}: {summary}")


if __name__ == "__main__":
    main()
