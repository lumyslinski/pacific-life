"""Unit tests of the framework-free parts of gea_api. No database, no web server."""
import json
import types
import unittest

from gea_api import errors, fields, http, runconfig, validation
from gea_api.errors import ApiError


class ETagTest(unittest.TestCase):
    def test_etag_is_stable_and_order_independent(self):
        self.assertEqual(http.etag_of({"a": 1, "b": [1, 2]}), http.etag_of({"b": [1, 2], "a": 1}))
        self.assertNotEqual(http.etag_of({"a": 1}), http.etag_of({"a": 2}))
        self.assertRegex(http.etag_of({"a": 1}), r'^"[A-Za-z0-9_-]{20}"$')

    def test_if_match(self):
        tag = http.etag_of({"a": 1})
        http.require_match(tag, tag, "x")
        http.require_match("*", tag, "x")
        http.require_match(f'"other", {tag}', tag, "x")
        with self.assertRaises(ApiError) as missing:
            http.require_match(None, tag, "x")
        self.assertEqual((missing.exception.status, missing.exception.code), (428, "precondition_required"))
        for stale in ('"other"', "W/" + tag, "nonsense"):          # a weak tag never satisfies If-Match
            with self.assertRaises(ApiError) as failed:
                http.require_match(stale, tag, "The run")
            self.assertEqual((failed.exception.status, failed.exception.code), (412, "precondition_failed"))

    def test_if_none_match(self):
        tag = http.etag_of({"a": 1})
        self.assertTrue(http.none_match(tag, tag))
        self.assertTrue(http.none_match("W/" + tag, tag))           # weak comparison
        self.assertTrue(http.none_match("*", tag))
        self.assertFalse(http.none_match('"other"', tag))
        self.assertFalse(http.none_match(None, tag))
        self.assertEqual(http.conditional({"a": 1}, tag).status, 304)
        self.assertEqual(http.conditional({"a": 1}, None).status, 200)


class MergePatchTest(unittest.TestCase):
    def test_rfc7396_examples(self):
        cases = [   # (target, patch, result): the table in RFC 7396 appendix A
            ({"a": "b"}, {"a": "c"}, {"a": "c"}),
            ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
            ({"a": "b"}, {"a": None}, {}),
            ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
            ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
            ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
            ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
            ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
            (["a", "b"], ["c", "d"], ["c", "d"]),
            ({"a": "b"}, ["c"], ["c"]),
            ({"a": "foo"}, None, None),
            ({"a": "foo"}, "bar", "bar"),
            ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
            ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
            ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
        ]
        for target, patch, expected in cases:
            self.assertEqual(http.merge_patch(target, patch), expected, (target, patch))

    def test_target_is_not_mutated(self):
        target = {"a": {"b": 1}}
        http.merge_patch(target, {"a": {"b": 2}})
        self.assertEqual(target, {"a": {"b": 1}})


class PagingTest(unittest.TestCase):
    def test_cursor_round_trip(self):
        cursor = http.encode_cursor("2026-10-02 05:35:00.123456+00", "3f0e3f6e-58a0-4a0b-9f5e-2a0c7d6f1b11")
        self.assertEqual(http.decode_cursor(cursor), ("2026-10-02 05:35:00.123456+00", "3f0e3f6e-58a0-4a0b-9f5e-2a0c7d6f1b11"))
        self.assertEqual(http.decode_cursor(None), (None, None))
        # The run log pages by its numeric id.
        log = http.encode_cursor("2026-10-02 05:35:00+00", "4711")
        self.assertEqual(http.decode_cursor(log, numeric_id=True), ("2026-10-02 05:35:00+00", "4711"))

    def test_bad_cursor_and_limit_are_validation_errors(self):
        for call in (lambda: http.decode_cursor("not-a-cursor"),
                     lambda: http.decode_cursor(http.encode_cursor("t", "4711")),               # a log cursor is not a list cursor
                     lambda: http.decode_cursor(http.encode_cursor("t", "1; DROP TABLE"), numeric_id=True),
                     lambda: http.page_limit("0"),
                     lambda: http.page_limit("101"), lambda: http.page_limit("ten")):
            with self.assertRaises(ApiError) as raised:
                call()
            self.assertEqual((raised.exception.status, raised.exception.code), (422, "validation_failed"))
        self.assertEqual((http.page_limit(None), http.page_limit("7")), (50, 7))

    def test_like_pattern_escapes_wildcards(self):
        self.assertEqual(http.like_pattern(" 50%_off\\ "), "50\\%\\_off\\\\")
        self.assertIsNone(http.like_pattern("  "))


class IdempotencyTest(unittest.TestCase):
    def test_key_is_required_and_bounded(self):
        self.assertEqual(http.idempotency_key(" abc "), "abc")
        for bad in (None, "  ", "x" * 256):
            with self.assertRaises(ApiError) as raised:
                http.idempotency_key(bad)
            self.assertEqual(raised.exception.status, 400)

    def test_fingerprint_depends_on_operation_path_and_body(self):
        base = http.request_fingerprint("createRun", "/p/1/runs", {"name": "a", "treaty": "t"})
        self.assertEqual(base, http.request_fingerprint("createRun", "/p/1/runs", {"treaty": "t", "name": "a"}))
        self.assertNotEqual(base, http.request_fingerprint("createRun", "/p/2/runs", {"name": "a", "treaty": "t"}))
        self.assertNotEqual(base, http.request_fingerprint("createRun", "/p/1/runs", {"name": "b", "treaty": "t"}))

    def test_request_id(self):
        self.assertEqual(http.request_id_from("abc-123.DEF_456"), "abc-123.DEF_456")
        for untrusted in (None, "short", "has space in it", "x" * 65, "new\nline-injected"):
            self.assertRegex(http.request_id_from(untrusted), r"^[0-9a-f]{32}$")


class ValidationTest(unittest.TestCase):
    def issues(self, call):
        with self.assertRaises(ApiError) as raised:
            call()
        self.assertEqual((raised.exception.status, raised.exception.code), (422, "validation_failed"))
        return {(i.get("pointer"), i["code"]) for i in raised.exception.issues}

    def test_create_project_reports_every_issue_at_once(self):
        found = self.issues(lambda: validation.validate(
            {"name": " ", "region": 5, "benefit": [], "periodFrom": "2026-13-01", "owner": "me", "colour": "red",
             "state": "signed-off"},
            validation.CREATE_PROJECT, validation.CREATE_PROJECT_REQUIRED))
        self.assertEqual(found, {("/name", "required"), ("/region", "invalid_type"), ("/benefit", "empty"),
                                 ("/periodFrom", "invalid_type"), ("/owner", "invalid_type"),
                                 ("/colour", "unknown_field"), ("/businessPurpose", "required"),
                                 ("/state", "unknown_option")})

    def test_valid_bodies_pass_unchanged(self):
        body = {"name": "Mortality 2026", "region": "europe", "businessPurpose": "pricing", "benefit": ["mortality"],
                "periodFrom": "2026-01-01", "periodTo": "2026-12-31", "owner": None, "inheritedFrom": None,
                "state": "draft"}
        self.assertIs(validation.validate(body, validation.CREATE_PROJECT, validation.CREATE_PROJECT_REQUIRED), body)
        validation.period_order("2026-01-01", "2026-12-31")
        self.assertEqual(self.issues(lambda: validation.period_order("2026-12-31", "2026-01-01")),
                         {("/periodTo", "invalid")})

    def test_merge_patch_bodies(self):
        validation.validate({"description": None, "cycle": None}, validation.UPDATE_PROJECT, at_least_one=True)
        self.assertEqual(self.issues(lambda: validation.validate({"name": None}, validation.UPDATE_PROJECT)),
                         {("/name", "invalid_type")})
        self.assertEqual(self.issues(lambda: validation.validate({}, validation.UPDATE_PROJECT, at_least_one=True)),
                         {(None, "required")})
        self.assertEqual(self.issues(lambda: validation.validate({"benefit": ["a", "a"]}, validation.UPDATE_PROJECT)),
                         {("/benefit", "invalid")})

    def test_job_and_resolution_bodies(self):
        run_a, run_b = "3f0e3f6e-58a0-4a0b-9f5e-2a0c7d6f1b11", "7a1d5c2e-9b44-4c0e-8a1f-0d2e4b6a8c10"
        body = {"name": "Batch", "note": None, "runIds": [run_a, run_b], "maxParallel": 1}
        self.assertIs(validation.validate(body, validation.CREATE_JOB, ("name", "runIds")), body)
        validation.validate({"name": "Batch", "runIds": [run_a, run_b], "maxParallel": None}, validation.CREATE_JOB)
        cases = {
            "one run is not a batch": ({"name": "B", "runIds": [run_a]}, {("/runIds", "invalid")}),
            "the same run twice": ({"name": "B", "runIds": [run_a, run_a.upper()]}, {("/runIds", "invalid")}),
            "not ids": ({"name": "B", "runIds": [run_a, "second"]}, {("/runIds", "invalid_type")}),
            "not a list": ({"name": "B", "runIds": run_a}, {("/runIds", "invalid_type")}),
            "limit below one": ({"name": "B", "runIds": [run_a, run_b], "maxParallel": 0}, {("/maxParallel", "invalid")}),
            "limit is not a number": ({"name": "B", "runIds": [run_a, run_b], "maxParallel": True}, {("/maxParallel", "invalid_type")}),
            "nothing": ({}, {("/name", "required"), ("/runIds", "required")}),
        }
        for label, (body, expected) in cases.items():
            self.assertEqual(self.issues(lambda: validation.validate(body, validation.CREATE_JOB, ("name", "runIds"))),
                             expected, label)
        validation.validate({"action": "mark-resolved", "note": ""}, validation.RESOLVE_RUN, ("action",))
        self.assertEqual(self.issues(lambda: validation.validate({"action": "reopen", "note": "x" * 2001},
                                                                 validation.RESOLVE_RUN, ("action",))),
                         {("/action", "unknown_option"), ("/note", "invalid")})
        self.assertEqual(self.issues(lambda: validation.validate({"note": "why"}, validation.RESOLVE_RUN, ("action",))),
                         {("/action", "required")})

    def test_user_administration_bodies(self):
        body = {"role": "preparer", "region": "asia"}
        self.assertIs(validation.validate(body, validation.ADMINISTER_USER, at_least_one=True), body)
        validation.validate({"region": "asia"}, validation.ADMINISTER_USER, at_least_one=True)
        cases = {
            "nothing to change": ({}, {(None, "required")}),
            "a role is a name, not a rank": ({"role": 3}, {("/role", "invalid_type")}),
            "no role is not a role": ({"role": None}, {("/role", "invalid_type")}),
            "permissions are the role's, not the user's": ({"permissions": {"administer": True}}, {("/permissions", "unknown_field")}),
        }
        for label, (body, expected) in cases.items():
            self.assertEqual(self.issues(lambda: validation.validate(body, validation.ADMINISTER_USER, at_least_one=True)),
                             expected, label)
        # The caller's own change is the home region and nothing else.
        self.assertEqual(self.issues(lambda: validation.validate({"role": "admin"}, validation.UPDATE_USER, ("region",))),
                         {("/role", "unknown_field"), ("/region", "required")})

    def test_body_must_be_an_object(self):
        for body in ([], "text", 3, None):
            for check in (lambda: validation.validate(body, validation.CREATE_PROJECT), lambda: runconfig.validate(body)):
                with self.assertRaises(ApiError) as raised:
                    check()
                self.assertEqual(raised.exception.status, 400)


class RunPropsTest(unittest.TestCase):
    """runconfig: the props of workbook sheet 'POC Data' between the API and the columns of gea."Run"."""

    def issues(self, call):
        with self.assertRaises(ApiError) as raised:
            call()
        self.assertEqual((raised.exception.status, raised.exception.code), (422, "validation_failed"))
        return {(i.get("pointer"), i["code"]) for i in raised.exception.issues}

    def test_every_prop_is_checked_against_its_workbook_type(self):
        found = self.issues(lambda: runconfig.validate({
            "dataScope": "Policy_v1",                                  # array<string>
            "studyPeriod": {"start": "2016-03-31"},                    # {start: date, end: date}
            "ibnrStudyPeriod": {"start": "2026-03-31", "end": "2016-03-31"},
            "studyPeriodExclusions": [{"start": "2019-03-31", "end": "2020-03-31"}, {"start": "x", "end": "y"}],
            "studyPeriodTreatyOverride": "Yes",                        # boolean
            "exposureMethod": 3,                                       # string
            "amountBasis": "  ",
            "exposureExclusion": ["a", "a"],
            "colour": "red"}))
        self.assertEqual(found, {
            ("/dataScope", "invalid_type"), ("/studyPeriod", "invalid_type"), ("/ibnrStudyPeriod", "invalid_type"),
            ("/studyPeriodExclusions/1", "invalid_type"), ("/studyPeriodTreatyOverride", "invalid_type"),
            ("/exposureMethod", "invalid_type"), ("/amountBasis", "required"), ("/exposureExclusion", "invalid"),
            ("/colour", "unknown_field")})

    def test_valid_props_are_cleaned(self):
        cleaned = runconfig.validate({"name": " Run ", "exposureMethod": " initial ", "dataScope": [],
                                      "studyPeriodExclusions": [], "investigation": None,
                                      "studyPeriod": {"start": "2016-03-31", "end": "2026-03-31"},
                                      "projectId": "p"}, also={"projectId": "uuid"})
        self.assertEqual(cleaned, {"name": "Run", "exposureMethod": "initial", "dataScope": None,
                                   "studyPeriodExclusions": None, "investigation": None,
                                   "studyPeriod": {"start": "2016-03-31", "end": "2026-03-31"}, "projectId": "p"})
        self.assertEqual(self.issues(lambda: runconfig.validate({"name": None, "treaty": None})),
                         {("/name", "required"), ("/treaty", "required")})
        self.assertEqual(self.issues(lambda: runconfig.validate({}, required=("projectId", "name"))),
                         {("/projectId", "required"), ("/name", "required")})
        self.assertEqual(self.issues(lambda: runconfig.validate({}, at_least_one=True)), {(None, "required")})

    def test_update_statement_writes_one_column_per_prop(self):
        statement, params = runconfig.update_statement({
            "exposureMethod": "initial", "eventMonthFilter": True, "dataScope": ["Policy_v1"],
            "studyPeriod": {"start": "2016-03-31", "end": "2026-03-31"}, "ibnrStudyPeriod": None,
            "studyPeriodExclusions": [{"start": "2019-03-31", "end": "2020-03-31"}], "outputFrequency": None})
        self.assertTrue(statement.startswith('UPDATE gea."Run" SET "ExposureMethod" = %(exposureMethod)s::text, '
                                             '"EventMonthFilter" = %(eventMonthFilter)s::boolean, "DataScope" = '))
        self.assertIn('"StudyPeriodStart" = %(studyPeriod_start)s::date, "StudyPeriodEnd" = %(studyPeriod_end)s::date',
                      statement)
        self.assertNotIn("Exclusion", statement)                       # rows of another table
        self.assertTrue(statement.endswith('"UpdatedBy" = %(user)s::uuid WHERE "Id" = %(id)s::uuid'))
        self.assertEqual(params, {
            "exposureMethod": "initial", "eventMonthFilter": True, "dataScope": '["Policy_v1"]',
            "studyPeriod_start": "2016-03-31", "studyPeriod_end": "2026-03-31",
            "ibnrStudyPeriod_start": None, "ibnrStudyPeriod_end": None, "outputFrequency": None})

    def test_representation_lists_every_prop_and_what_is_missing(self):
        body = {"id": "r", "projectId": "p", "projectName": "P", "name": "Run", "treaty": "T", "region": "europe",
                "status": "draft", "locked": False, "cloneSourceId": None, "currentContract": None, "submittedAt": None,
                "submittedBy": None, "failureMessage": None, "jobId": None, "jobOrdinal": None, "cancelRequestedAt": None,
                "resolution": None, "revision": 1, "createdAt": "t", "updatedAt": "t"}
        run = runconfig.represent(body, {"dataScope": ["Policy_v1"], "studyPeriodTreatyOverride": True})
        self.assertEqual([f.prop for f in fields.RUN_CONFIG], [name for name in run if name in fields.RUN_FIELD and name
                                                               not in ("name", "treaty")])
        self.assertIsNone(run["investigation"])
        missing = [i["field"] for i in run["issues"]]
        # In workbook order; perTreatyEndDatesMapping is required because the override is on.
        self.assertEqual(missing, [f.prop for f in fields.RUN_CONFIG
                                   if (f.required and f.prop not in ("dataScope", "studyPeriodTreatyOverride"))
                                   or f.prop == "perTreatyEndDatesMapping"])
        self.assertEqual([s["key"] for s in run["steps"]], [key for key, _ in fields.STEPS])
        self.assertEqual({s["key"] for s in run["steps"] if s["status"] == "complete"}, {"main", "assigningExpected"})
        self.assertFalse(run["ready"])
        summary = runconfig.summarise(body, {"dataScope": ["Policy_v1"]})
        self.assertEqual((summary["steps"], summary["completeSteps"], summary["ready"]), (8, 2, False))
        resolved = {**body, "status": "failed", "jobId": "j", "jobOrdinal": 2,
                    "resolution": {"action": "mark-resolved", "note": None, "resolvedBy": {"id": "u", "name": "U"}, "resolvedAt": "t"}}
        self.assertEqual(runconfig.represent(resolved, {})["resolution"]["action"], "mark-resolved")
        summary = runconfig.summarise(resolved, {})
        self.assertEqual((summary["jobId"], summary["jobOrdinal"], summary["resolutionAction"]), ("j", 2, "mark-resolved"))


def database_error(sqlstate, message="boom", detail=None, constraint=None):
    error = Exception(message)
    error.sqlstate = sqlstate
    error.diag = types.SimpleNamespace(message_primary=message, message_detail=detail, constraint_name=constraint)
    return error


class ApiErrorTest(unittest.TestCase):
    def test_body_is_the_error_object_of_the_envelope(self):
        error = errors.validation_failed([errors.issue("required", "name is required.", field="name", pointer="/name")])
        self.assertEqual(error.body(request_id="req-1"), {
            "code": "validation_failed", "message": "name is required.", "status": 422,
            "issues": [{"code": "required", "message": "name is required.", "field": "name", "pointer": "/name"}],
            "requestId": "req-1"})
        self.assertEqual(errors.precondition_required().body(request_id="r")["issues"], [])     # always a list

    def test_every_code_of_the_contract_has_a_default_message(self):
        import yaml                                   # the enum in the contract is the source of truth
        from pathlib import Path
        spec = yaml.safe_load((Path(__file__).resolve().parents[2] / "gea/api/openapi.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(spec["components"]["schemas"]["ErrorCode"]["enum"]), set(errors.MESSAGES))

    def test_sqlstate_mapping(self):
        expectations = {
            "GEA03": (409, "locked"), "GEA04": (409, "invalid_state"), "GEA05": (412, "precondition_failed"),
            "GEA06": (403, "forbidden"),
            "23503": (422, "validation_failed"), "23514": (422, "validation_failed"), "22P02": (422, "validation_failed"),
            "40P01": (503, "service_unavailable"), "57014": (503, "service_unavailable"),
            "08006": (503, "service_unavailable"), "XX000": (500, "internal_error"),
        }
        for state, expected in expectations.items():
            error = errors.from_database_error(database_error(state))
            self.assertEqual((error.status, error.code), expected, state)
        # The database's backstop for a submit with a required prop still empty.
        not_ready = errors.from_database_error(database_error("23514", constraint="CK_Run_RequiredWhenSubmitted"))
        self.assertEqual((not_ready.status, not_ready.code), (422, "not_ready"))
        self.assertEqual(errors.from_database_error(database_error("40001")).headers, {"Retry-After": "2"})

    def test_internal_errors_do_not_leak_the_database_message(self):
        error = errors.from_database_error(database_error("XX000", 'relation "secret_table" does not exist'))
        self.assertNotIn("secret_table", json.dumps(error.body(request_id="r")))


if __name__ == "__main__":
    unittest.main()
