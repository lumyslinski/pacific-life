"""The statement splitter used by the Alembic revisions, and the revision files themselves."""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "gea" / "db" / "postgres" / "migrations"
sys.path.insert(0, str(MIGRATIONS))

import gea_sql  # noqa: E402


class SplitStatementsTest(unittest.TestCase):
    def test_plain_statements(self):
        self.assertEqual(gea_sql.split_statements("SELECT 1; SELECT 2 ;\n"), ["SELECT 1", "SELECT 2"])
        self.assertEqual(gea_sql.split_statements("SELECT 1"), ["SELECT 1"])
        self.assertEqual(gea_sql.split_statements("  \n-- only a comment;\n/* and ; another */\n"), [])

    def test_semicolons_that_do_not_end_a_statement(self):
        script = """
        -- a comment; with a semicolon
        CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable; really', TG_ARGV[0];   -- ; in a comment
            RETURN NEW;
        END $$;
        INSERT INTO t VALUES ('it''s; fine', "odd;name", $tag$ body; $$ still body $tag$);
        /* block; /* nested; */ still comment; */ SELECT ';' AS "a;b";
        SELECT E'escaped \\' quote; here';
        """
        statements = gea_sql.split_statements(script)
        self.assertEqual(len(statements), 4)
        self.assertTrue(statements[0].endswith("END $$"))
        self.assertIn("$tag$ body; $$ still body $tag$", statements[1])
        self.assertTrue(statements[2].endswith('SELECT \';\' AS "a;b"'))
        self.assertIn("quote; here", statements[3])

    def test_positional_parameters_are_not_dollar_quotes(self):
        self.assertEqual(len(gea_sql.split_statements("PREPARE p AS SELECT $1, $2; EXECUTE p(1, 2);")), 2)

    def test_unterminated_constructs_are_errors(self):
        for broken in ("SELECT 'open", "SELECT $$ open", 'SELECT "open', "/* open"):
            with self.assertRaises(ValueError):
                gea_sql.split_statements(broken)


class RevisionFilesTest(unittest.TestCase):
    def revisions(self):
        found = {}
        for path in sorted((MIGRATIONS / "versions").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            revision = re.search(r'^revision = "(\w+)"$', text, re.M).group(1)
            down = re.search(r'^down_revision = (None|"(\w+)")$', text, re.M).group(2)
            self.assertTrue(path.stem.startswith(revision + "_"), path.name)
            found[revision] = (down, path)
        return found

    def test_one_linear_history(self):
        revisions = self.revisions()
        self.assertEqual([down for down, _ in revisions.values()].count(None), 1, "exactly one base revision")
        parents = [down for down, _ in revisions.values() if down is not None]
        self.assertEqual(len(parents), len(set(parents)), "a revision has two children (branch)")
        self.assertTrue(set(parents) <= set(revisions), "down_revision points at a missing revision")

    def test_every_revision_has_up_and_down_sql(self):
        for _, path in self.revisions().values():
            for direction in ("up", "down"):
                statements = gea_sql.statements_for(str(path), direction)
                self.assertTrue(statements, f"{path.stem}.{direction}.sql is empty")
                for statement in statements:
                    head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
                    self.assertNotIn(head, {"BEGIN", "COMMIT", "ROLLBACK"}, f"{path.stem}: Alembic owns the transaction")
                    self.assertFalse(re.match(r"\s*SET\s+search_path", statement, re.I),
                                     f"{path.stem}: qualify objects with gea. instead of SET search_path")
                    self.assertFalse(re.search(r"^\s*\\\w", statement, re.M), f"{path.stem}: psql meta-command")

    def test_no_orphan_sql_files(self):
        stems = {path.stem for _, path in self.revisions().values()}
        for sql in (MIGRATIONS / "sql").glob("*.sql"):
            self.assertIn(sql.name.rsplit(".", 2)[0], stems, f"{sql.name} belongs to no revision")



class WorkbookFieldsTest(unittest.TestCase):
    """Every prop of workbook sheet 'POC Data' (gea/spec/poc-data.json) is a column of the schema."""

    SPEC = json.loads((ROOT / "gea/spec/poc-data.json").read_text(encoding="utf-8"))
    SQL = (MIGRATIONS / "sql" / "0002_project_and_run.up.sql").read_text(encoding="utf-8")
    TYPES = {"string": "text", "boolean": "boolean", "array<string>": "text[]"}

    def table(self, name):
        start = self.SQL.index(f'CREATE TABLE gea."{name}" (')
        return self.SQL[start:self.SQL.index("\n);", start)]

    def test_run_has_one_typed_column_per_prop(self):
        run = self.table("Run")
        for field in self.SPEC["createRun"]:
            if "table" in field:
                self.assertIn(f'CREATE TABLE gea."{field["table"]}" (', self.SQL, field["prop"])
                continue
            for column in field.get("columns", [field.get("column")]):
                kind = "date" if "columns" in field else self.TYPES[field["type"]]
                self.assertRegex(run, rf'\n    "{column}"\s+{re.escape(kind)}[ ,]', f'createRun.{field["prop"]}')
                self.assertIn(f"-- createRun.{field['prop']}", run)

    def test_required_props_are_in_the_submit_check(self):
        run = self.table("Run")
        check = run[run.index('CONSTRAINT "CK_Run_RequiredWhenSubmitted"'):]
        for field in self.SPEC["createRun"]:
            if field["step"] == "main":
                continue                                               # name and treaty are NOT NULL columns
            columns = field.get("columns", [field.get("column")] if "column" in field else [])
            for column in columns:
                self.assertEqual(f'"{column}"' in check, field["required"] or "requiredWhen" in field
                                 or any(f.get("requiredWhen", {}).get("prop") == field["prop"] for f in self.SPEC["createRun"]),
                                 f'createRun.{field["prop"]}')

    def test_contract_steps_carry_every_prop_under_its_workbook_name(self):
        start = self.SQL.index('CREATE FUNCTION gea."RunSteps"')
        function = self.SQL[start:self.SQL.index("$$;", start)]
        steps = [s["key"] for s in self.SPEC["steps"] if s["key"] != "main"]
        self.assertEqual(re.findall(r"jsonb_build_object\('key', '(\w+)', 'ordinal', (\d+)", function),
                         [(key, str(n)) for n, key in enumerate(steps, 1)])
        for field in self.SPEC["createRun"]:
            if field["step"] != "main":
                self.assertEqual(function.count(f"'{field['prop']}', "), 1, field["prop"])

    def test_project_columns_and_lookups(self):
        project = self.table("Project")
        for field in self.SPEC["createProject"]:
            if "column" in field:
                self.assertRegex(project, rf'\n    "{field["column"]}"\s+text NOT NULL', field["prop"])
            else:
                self.assertIn(f'CREATE TABLE gea."{field["table"]}" (', self.SQL)
        baseline = (MIGRATIONS / "sql" / "0001_baseline.up.sql").read_text(encoding="utf-8")
        for field in self.SPEC["createProject"]:
            if "lookup" in field:
                values = re.search(rf'INSERT INTO gea."{field["lookup"]}" \("Id", "Name", "SortOrder"\) VALUES(.*?);', baseline, re.S)
                self.assertEqual(re.findall(r"\('([^']+)', '([^']+)', \d+\)", values.group(1)),
                                 [tuple(option) for option in field["options"]], field["lookup"])

    def test_dropdown_examples_are_seeded(self):
        seed = self.SQL[self.SQL.index('INSERT INTO gea."ParameterOption"'):]
        rows = re.findall(r"\('(\w+)', '([^']+)', '([^']+)', (\d+)\)", seed)
        expected = [(f["prop"], value, label, str(n)) for f in self.SPEC["createRun"]
                    for n, (value, label) in enumerate(f.get("options") or [], 1)]
        self.assertEqual(rows, expected)

    def test_names_have_no_underscores(self):
        """Tables and columns carry the workbook's names: PascalCase, quoted."""
        for path in sorted((MIGRATIONS / "sql").glob("*.up.sql")):
            text = path.read_text(encoding="utf-8")
            for table, body in re.findall(r'CREATE TABLE (\S+) \((.*?)\n\);', text, re.S):
                self.assertRegex(table, r'^(gea|calc)\."[A-Z][A-Za-z]+"$', path.name)
                columns = re.findall(r'^    ("?\w+"?)\s+(?:text|uuid|date|boolean|integer|smallint|bigint|jsonb|timestamptz|char)',
                                     body, re.M)
                self.assertTrue(columns, table)
                for column in columns:
                    self.assertRegex(column, r'^"[A-Z][A-Za-z]+"$', f"{table}: {column}")
            for column in re.findall(r'ADD COLUMN (\S+)', text):                # columns added by a later revision
                self.assertRegex(column, r'^"[A-Z][A-Za-z]+"$', path.name)
            for view in re.findall(r'CREATE (?:OR REPLACE )?VIEW (\S+) AS', text):
                self.assertRegex(view, r'^gea\."[A-Z][A-Za-z]+"$', path.name)


if __name__ == "__main__":
    unittest.main()
