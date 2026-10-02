#!/usr/bin/env bash
# Migrate a scratch database from zero with Alembic and run the smoke test against it.
#   GEA_ADMIN_URL=postgresql://postgres@localhost:5432/postgres bash gea/db/postgres/tests/run.sh
# Needs psql, pg_dump and python. Creates and drops the database gea_test. Revisions 0004 and 0005 create the cluster-wide
# roles gea_app, gea_relay and calc_audit when they are missing, so the admin user needs CREATEROLE.
set -euo pipefail
: "${GEA_ADMIN_URL:?Set GEA_ADMIN_URL to an administrator connection, e.g. postgresql://postgres@localhost:5432/postgres}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
alembic=(alembic -c "$here/../alembic.ini")

psql "$GEA_ADMIN_URL" -X -q -v ON_ERROR_STOP=1 <<'SQL'
SET client_min_messages = warning;
DROP DATABASE IF EXISTS gea_test;
CREATE DATABASE gea_test;
SQL
export GEA_DATABASE_URL="${GEA_ADMIN_URL%/*}/gea_test"

"${alembic[@]}" upgrade head
"${alembic[@]}" upgrade head                 # a second run finds nothing to do
"${alembic[@]}" downgrade base               # every revision has a working downgrade

# Every revision on its own: applying it twice gives the same schema, and its downgrade leaves
# exactly the schema of the revision before it (a revision that replaces a function, trigger or
# view of an earlier one has to put the earlier text back).
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
dump() { pg_dump "$GEA_DATABASE_URL" --schema=gea --schema=calc --schema-only --no-owner | grep -v '^\\'; }
previous=base
for file in "$here"/../migrations/versions/[0-9]*.py; do
    revision="$(basename "$file")"; revision="${revision%%_*}"
    "${alembic[@]}" upgrade "$revision";    dump > "$tmp/$revision.sql"
    "${alembic[@]}" downgrade "$previous";  dump > "$tmp/back.sql"
    [[ "$previous" == base ]] || diff -q "$tmp/$previous.sql" "$tmp/back.sql" > /dev/null \
        || { echo "the downgrade of $revision does not restore the schema of $previous" >&2; exit 1; }
    "${alembic[@]}" upgrade "$revision";    dump > "$tmp/again.sql"
    diff -q "$tmp/$revision.sql" "$tmp/again.sql" > /dev/null \
        || { echo "revision $revision gives a different schema when it is applied again" >&2; exit 1; }
    previous="$revision"
done
[[ "$("${alembic[@]}" current 2>/dev/null | awk '{print $1}')" == "$("${alembic[@]}" heads | awk '{print $1}')" ]] \
    || { echo "database is not at the head revision" >&2; exit 1; }

psql "$GEA_DATABASE_URL" -X -v ON_ERROR_STOP=1 -f "$here/smoke.sql"
# The export for Lucidchart is committed: it has to be the schema the migrations give.
"$(command -v python3 || command -v python)" "$here/../../../tools/lucid_export.py" --database-url "$GEA_DATABASE_URL" --check
psql "$GEA_ADMIN_URL" -X -q -c "DROP DATABASE gea_test"
