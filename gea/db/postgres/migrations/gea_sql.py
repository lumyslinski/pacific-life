"""Run the SQL of a revision through Alembic, one statement at a time.

Each revision `versions/<id>_<slug>.py` has two files next to it in `sql/`:
`<id>_<slug>.up.sql` and `<id>_<slug>.down.sql`. Keeping the SQL in .sql files
means it is reviewed, highlighted and linted as SQL, and can be tried in psql.

The files are plain SQL: no psql meta-commands (\\i, \\set, \\gset) and no
transaction control (BEGIN, COMMIT). Alembic owns the transaction.
"""
from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"

# What sqlalchemy.text() would read as a bind parameter, e.g. the ":true" in
# '{"equals":true}'. Escaping it with a backslash makes text() pass it through.
_BIND = re.compile(r"(?<![:\w\\]):(\w+)(?!:)")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")


def split_statements(script: str) -> list[str]:
    """Split a SQL script on the semicolons that end statements.

    Semicolons inside string literals, quoted identifiers, dollar-quoted bodies
    ($$ ... $$, $tag$ ... $tag$) and comments are not statement ends. A chunk
    that holds only whitespace and comments is dropped.
    """
    statements: list[str] = []
    start = position = 0
    has_code = False
    length = len(script)

    def close(end: int) -> None:
        nonlocal start, has_code
        if has_code:
            statements.append(script[start:end].strip())
        start, has_code = end + 1, False

    while position < length:
        char = script[position]
        pair = script[position:position + 2]
        if pair == "--":
            newline = script.find("\n", position)
            position = length if newline == -1 else newline + 1
        elif pair == "/*":
            depth, position = 1, position + 2              # block comments nest in PostgreSQL
            while depth and position < length:
                if script.startswith("/*", position):
                    depth, position = depth + 1, position + 2
                elif script.startswith("*/", position):
                    depth, position = depth - 1, position + 2
                else:
                    position += 1
            if depth:
                raise ValueError("unterminated block comment")
        elif char == "'":
            has_code = True
            escapes = (position > 0 and script[position - 1] in "eE"
                       and not (position > 1 and (script[position - 2].isalnum() or script[position - 2] == "_")))
            position += 1
            while True:
                if position >= length:
                    raise ValueError("unterminated string literal")
                if escapes and script[position] == "\\":
                    position += 2
                elif script[position] == "'":
                    if script.startswith("''", position):
                        position += 2
                    else:
                        position += 1
                        break
                else:
                    position += 1
        elif char == '"':
            has_code = True
            end = script.find('"', position + 1)
            while end != -1 and script.startswith('""', end):
                end = script.find('"', end + 2)
            if end == -1:
                raise ValueError("unterminated quoted identifier")
            position = end + 1
        elif char == "$" and not (position > 0 and (script[position - 1].isalnum() or script[position - 1] == "_")):
            has_code = True
            tag = _DOLLAR_TAG.match(script, position)
            if tag is None:
                position += 1                               # a positional parameter such as $1
                continue
            end = script.find(tag.group(0), tag.end())
            if end == -1:
                raise ValueError(f"unterminated dollar-quoted string {tag.group(0)}")
            position = end + len(tag.group(0))
        elif char == ";":
            close(position)
            position += 1
        else:
            has_code = has_code or not char.isspace()
            position += 1
    close(length)
    return statements


def sql_path(revision_file: str, direction: str) -> Path:
    """sql/<revision file stem>.<up|down>.sql for a file in versions/."""
    if direction not in ("up", "down"):
        raise ValueError("direction is 'up' or 'down'")
    return SQL_DIR / f"{Path(revision_file).stem}.{direction}.sql"


def statements_for(revision_file: str, direction: str) -> list[str]:
    path = sql_path(revision_file, direction)
    statements = split_statements(path.read_text(encoding="utf-8"))
    if not statements:
        raise ValueError(f"{path.name} contains no SQL statement")
    return statements


def run_sql(revision_file: str, direction: str) -> None:
    """Call as run_sql(__file__, "up") or run_sql(__file__, "down") from a revision."""
    from alembic import op

    for statement in statements_for(revision_file, direction):
        op.execute(_BIND.sub(r"\\:\1", statement))
