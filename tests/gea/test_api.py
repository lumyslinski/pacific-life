"""The GEA API end to end: HTTP in, PostgreSQL underneath, HTTP out.

Needs a migrated database:

    export GEA_DATABASE_URL=postgresql://postgres@localhost:5432/gea_test
    alembic -c gea/db/postgres/alembic.ini upgrade head
    GEA_TEST_DATABASE_URL=$GEA_DATABASE_URL python -m pytest tests/gea        (or: python -m unittest discover tests/gea)

Without GEA_TEST_DATABASE_URL the tests are skipped. Every test creates its own
project, so the suite can run repeatedly against the same database.
"""
import hashlib
import json
import os
import unittest
import uuid
from pathlib import Path

import yaml

DATABASE_URL = os.environ.get("GEA_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[2]
JSON = "application/json"

# A complete, valid run: the props of gea/spec/data-contract.example.json, grouped by step and flat.
EXAMPLE = json.loads((ROOT / "gea/spec/data-contract.example.json").read_text(encoding="utf-8"))
STEPS = {step["key"]: step["config"] for step in EXAMPLE["steps"]}
CONFIG = {prop: value for config in STEPS.values() for prop, value in config.items()}
SPEC = json.loads((ROOT / "gea/spec/poc-data.json").read_text(encoding="utf-8"))
RUN_PROPS = [f["prop"] for f in SPEC["createRun"]]


def new_key() -> str:
    return str(uuid.uuid4())


@unittest.skipUnless(DATABASE_URL, "set GEA_TEST_DATABASE_URL to a migrated database")
class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from litestar.testing import TestClient

        from gea_api.app import create_app
        from gea_api.config import Settings

        cls.app = create_app(Settings(database_url=DATABASE_URL, auth_mode="dev"))
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    # ------------------------------------------------------------------ helpers
    def call(self, method, path, body=None, *, merge=False, user="tester", **headers):
        """One request. Keyword arguments become headers: if_match="..." -> If-Match."""
        sent = {"X-Dev-User": user}
        sent.update({name.replace("_", "-").title(): value for name, value in headers.items() if value is not None})
        if body is None:
            return self.client.request(method, "/gea/v1" + path, headers=sent)
        sent["Content-Type"] = "application/merge-patch+json" if merge else "application/json"
        return self.client.request(method, "/gea/v1" + path, content=json.dumps(body).encode(), headers=sent)

    def ok(self, response, status=200):
        """A success: the envelope with `data` and a null `error`. Returns `data`."""
        self.assertEqual(response.status_code, status, response.content)
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertTrue(response.headers["content-type"].startswith(JSON))
        body = response.json()
        self.assertEqual(set(body), {"data", "error"})
        self.assertIsNone(body["error"])
        return body["data"]

    def failure(self, response, status, code):
        """A failure: the same envelope with a null `data` and the one error object. Returns `error`."""
        self.assertEqual(response.status_code, status, response.content)
        self.assertTrue(response.headers["content-type"].startswith(JSON))
        body = response.json()
        self.assertEqual(set(body), {"data", "error"})
        self.assertIsNone(body["data"])
        error = body["error"]
        self.assertEqual(set(error), {"code", "message", "status", "issues", "requestId"})
        self.assertEqual((error["status"], error["code"]), (status, code))
        self.assertTrue(error["message"])
        self.assertIsInstance(error["issues"], list)
        self.assertEqual(error["requestId"], response.headers["x-request-id"])
        return error

    def new_project(self, **overrides):
        body = {"name": f"Test {uuid.uuid4().hex[:8]}", "region": "europe", "businessPurpose": "pricing",
                "benefit": ["mortality"], **overrides}
        response = self.call("POST", "/projects", body, idempotency_key=new_key())
        return self.ok(response, 201), response.headers["etag"]

    def new_run(self, project_id, **overrides):
        body = {"projectId": project_id, "name": "Mortality study", "treaty": "TRT-001", **overrides}
        response = self.call("POST", "/runs", body, idempotency_key=new_key())
        return self.ok(response, 201), response.headers["etag"]

    def complete_run(self):
        project, _ = self.new_project()
        run, _ = self.new_run(project["id"], **CONFIG)
        return run["id"]

    def submit(self, run_id, key=None):
        review = self.call("GET", f"/runs/{run_id}/review")
        return self.call("POST", f"/runs/{run_id}/submit", idempotency_key=key or new_key(),
                         if_match=review.headers["etag"])

    def relay(self, run_id, status=None, steps=None, message=None):
        """What the relay does for one run: deliver its contract and, with `status`, report from Snowflake."""
        contract = self.ok(self.call("GET", f"/runs/{run_id}"))["currentContract"]["contractId"]
        self.sql('SELECT "ContractId"::text AS id FROM gea."ClaimContractDeliveries"(\'test-relay\', 1000)')
        self.sql('SELECT gea."CompleteContractDelivery"(%(id)s::uuid, \'snowflake\', \'test-relay\', \'query-1\') AS done',
                 id=contract)
        if status:
            self.sql('SELECT gea."RecordExecutionStatus"(%(id)s::uuid, %(status)s::text, %(steps)s::jsonb, %(message)s::text) AS s',
                     id=contract, status=status, steps=json.dumps(steps or []), message=message)
        return contract

    def owner_only(self, statement, **params):
        """Reference data is written by the schema owner, not by the API's role: skip when the login may not."""
        try:
            return self.sql(statement, **params)
        except Exception as error:
            if getattr(error, "sqlstate", None) != "42501":
                raise
            self.skipTest("GEA_TEST_DATABASE_URL is a login that may not write gea.\"ParameterOption\"")

    def sql(self, statement, **params):
        with self.app.state.gea.db.transaction() as connection:
            return connection.execute(statement, params).fetchall()

    def person(self, role):
        """A user seen for the first time with this role. Returns (the name to call as, the user)."""
        name = f"{role}-{uuid.uuid4().hex[:8]}"
        return name, self.ok(self.call("GET", "/users/me", user=name, x_dev_role=role))

    # ------------------------------------------------------------------ health, reference data, run parameters
    def test_health_and_reference_data(self):
        health = self.client.request("GET", "/gea/v1/health")
        self.assertEqual(health.json(), {"data": {"status": "ok", "service": "gea-api", "specVersion": "1.0"},
                                         "error": None})
        self.ok(health)
        # The lists of the project form: Project.Region, Project.BusinessPurpose, Project.Benefit of the workbook.
        regions = self.ok(self.call("GET", "/regions"))
        self.assertEqual([r["id"] for r in regions["regions"]], [value for value, _ in SPEC["createProject"][1]["options"]])
        self.assertIn({"id": "north-america", "name": "North America"}, regions["regions"])
        self.assertEqual(self.ok(self.call("GET", "/business-purposes")),
                         {"businessPurposes": [{"id": "rnd", "name": "R&D"}, {"id": "pricing", "name": "Pricing"}]})
        self.assertEqual([b["id"] for b in self.ok(self.call("GET", "/benefits"))["benefits"]],
                         [value for value, _ in SPEC["createProject"][3]["options"]])
        self.assertEqual(self.ok(self.call("GET", "/datasets")),
                         {"datasets": [{"id": "Policy_v1", "name": "Policy_v1"}, {"id": "Claims_v1", "name": "Claims_v1"}]})
        self.assertIsInstance(self.ok(self.call("GET", "/treaties?region=europe"))["treaties"], list)
        self.failure(self.call("GET", "/treaties?region=atlantis"), 422, "validation_failed")
        self.failure(self.call("GET", "/profile"), 404, "not_found")                           # the old name is gone

    def test_run_parameters_of_a_project(self):
        project, _ = self.new_project()
        parameters = self.ok(self.call("GET", f"/projects/{project['id']}/run-parameters"))
        self.assertEqual(parameters["scope"], {"region": "europe", "businessPurpose": "pricing",
                                               "benefit": ["mortality"], "investigation": None})
        # Every createRun field of sheet 'POC Data', in workbook order, under its workbook name.
        self.assertEqual([p["name"] for p in parameters["parameters"]], RUN_PROPS)
        by_name = {p["name"]: p for p in parameters["parameters"]}
        scope = by_name["dataScope"]
        self.assertEqual((scope["displayName"], scope["step"], scope["type"], scope["control"], scope["required"]),
                         ("Data Scope", "dataAndSetUp", "stringArray", "multiselect", True))
        self.assertEqual(scope["options"], [{"value": "Policy_v1", "label": "Policy_v1"},
                                            {"value": "Claims_v1", "label": "Claims_v1"}])
        self.assertEqual(by_name["perTreatyEndDatesMapping"]["requiredWhen"],
                         {"prop": "studyPeriodTreatyOverride", "equals": True})
        self.assertEqual(by_name["ibnrStudyPeriod"]["defaultFrom"], "studyPeriod")
        self.assertIsNone(by_name["studyPeriod"]["options"])                                  # not a dropdown
        self.assertEqual(by_name["ibnrMethodology"]["default"], "chain-ladder")               # the only value offered
        self.failure(self.call("GET", f"/projects/{uuid.uuid4()}/run-parameters"), 404, "not_found")

    def test_every_failure_is_the_same_envelope(self):
        self.failure(self.call("GET", "/no-such-route"), 404, "not_found")
        self.failure(self.call("GET", f"/projects/{uuid.uuid4()}"), 404, "not_found")
        self.failure(self.call("GET", "/projects/not-a-uuid"), 404, "not_found")
        self.failure(self.call("GET", "/projects?limit=1000"), 422, "validation_failed")
        self.failure(self.call("GET", "/projects?state=archived"), 422, "validation_failed")
        self.failure(self.call("GET", "/runs?projectId=nope"), 422, "validation_failed")
        self.failure(self.call("POST", "/projects", {"name": "x"}), 400, "bad_request")       # no Idempotency-Key
        malformed = self.client.request("POST", "/gea/v1/projects", content=b"{not json",
                                        headers={"Content-Type": "application/json", "Idempotency-Key": new_key()})
        self.failure(malformed, 400, "bad_request")

    def test_request_id_is_echoed(self):
        response = self.call("GET", "/datasets", x_request_id="support-ticket-4711")
        self.assertEqual(response.headers["x-request-id"], "support-ticket-4711")
        missing = self.failure(self.call("GET", f"/projects/{uuid.uuid4()}", x_request_id="support-ticket-4712"),
                               404, "not_found")
        self.assertEqual((missing["requestId"], missing["issues"]), ("support-ticket-4712", []))

    def test_an_unexpected_exception_is_an_envelope_without_details(self):
        from gea_api import services

        real = services.datasets

        def broken(call):
            raise RuntimeError("secret internal detail")

        services.datasets = broken
        try:
            import logging
            logging.getLogger("gea_api").disabled = True
            error = self.failure(self.call("GET", "/datasets"), 500, "internal_error")
        finally:
            logging.getLogger("gea_api").disabled = False
            services.datasets = real
        self.assertNotIn("secret", json.dumps(error))

    # ------------------------------------------------------------------ users
    def test_a_user_has_a_home_region(self):
        name = f"user-{uuid.uuid4().hex[:8]}"
        me = self.ok(self.call("GET", "/users/me", user=name, x_dev_region="asia"))
        self.assertEqual((me["name"], me["region"], me["regionName"]), (name, "asia", "Asia"))
        # The region is taken when the user is first seen; later requests do not move the user.
        self.assertEqual(self.ok(self.call("GET", "/users/me", user=name, x_dev_region="europe"))["region"], "asia")
        # Without a region from the identity provider a new user starts in Global.
        other = self.ok(self.call("GET", "/users/me", user=f"user-{uuid.uuid4().hex[:8]}"))
        self.assertEqual(other["region"], "global")
        etag = self.call("GET", "/users/me", user=name).headers["etag"]
        self.failure(self.call("PATCH", "/users/me", {"region": "europe"}, merge=True, user=name), 428, "precondition_required")
        moving = self.call("PATCH", "/users/me", {"region": "europe"}, merge=True, user=name, if_match=etag)
        moved = self.ok(moving)
        self.assertEqual((moved["id"], moved["region"]), (me["id"], "europe"))
        unknown = self.failure(self.call("PATCH", "/users/me", {"region": "atlantis"}, merge=True, user=name,
                                         if_match=moving.headers["etag"]), 422, "validation_failed")
        self.assertEqual(unknown["issues"][0]["pointer"], "/region")
        row = self.sql('SELECT "RegionId" AS region FROM gea."User" WHERE "Id" = %(id)s::uuid', id=me["id"])
        self.assertEqual(row[0]["region"], "europe")

    # ------------------------------------------------------------------ roles
    def test_a_user_has_a_role_and_the_permissions_of_that_role(self):
        roles = self.ok(self.call("GET", "/roles"))["roles"]
        self.assertEqual([(r["id"], r["name"]) for r in roles],
                         [("viewer", "Viewer"), ("preparer", "Preparer"), ("reviewer", "Reviewer"), ("admin", "Admin")])
        allowed = {r["id"]: sorted(name for name, yes in r["permissions"].items() if yes) for r in roles}
        self.assertEqual(allowed, {"viewer": [], "preparer": ["prepare"], "reviewer": ["prepare", "review"],
                                   "admin": ["administer", "prepare", "review"]})
        for role in roles:
            name, me = self.person(role["id"])
            self.assertEqual((me["role"], me["roleName"], me["permissions"]), (role["id"], role["name"], role["permissions"]))
        # The role is taken when the user is first seen; a later request does not raise it.
        name, viewer = self.person("viewer")
        again = self.ok(self.call("GET", "/users/me", user=name, x_dev_role="admin"))
        self.assertEqual((again["id"], again["role"]), (viewer["id"], "viewer"))
        # A role the identity provider invents is not a role: the user starts as a Viewer.
        unknown = self.ok(self.call("GET", "/users/me", user=f"user-{uuid.uuid4().hex[:8]}", x_dev_role="owner"))
        self.assertEqual((unknown["role"], unknown["permissions"]), ("viewer", {"prepare": False, "review": False, "administer": False}))
        # A role is not something a user gives themselves.
        etag = self.call("GET", "/users/me", user=name).headers["etag"]
        refused = self.failure(self.call("PATCH", "/users/me", {"role": "admin"}, merge=True, user=name, if_match=etag),
                               422, "validation_failed")
        self.assertEqual([i["code"] for i in refused["issues"]], ["unknown_field", "required"])

    def test_a_viewer_reads_and_a_preparer_works(self):
        viewer, _ = self.person("viewer")
        preparer, _ = self.person("preparer")
        reviewer, _ = self.person("reviewer")
        body = {"name": f"Roles {uuid.uuid4().hex[:8]}", "region": "europe", "businessPurpose": "pricing", "benefit": ["mortality"]}

        refused = self.failure(self.call("POST", "/projects", body, user=viewer, idempotency_key=new_key()), 403, "forbidden")
        self.assertEqual(refused["message"],
                         "Your role (Viewer) does not allow this. It takes a role that may prepare projects and runs.")
        self.assertEqual(self.ok(self.call("GET", f"/projects?q={body['name']}", user=viewer))["projects"], [])

        created = self.call("POST", "/projects", body, user=preparer, idempotency_key=new_key())
        project = self.ok(created, 201)
        run = self.ok(self.call("POST", "/runs", {"projectId": project["id"], "name": "By the preparer", "treaty": "TRT-001", **CONFIG},
                                user=preparer, idempotency_key=new_key()), 201)
        # The viewer sees all of it and changes none of it.
        self.assertEqual(self.ok(self.call("GET", f"/projects/{project['id']}", user=viewer))["id"], project["id"])
        self.assertEqual(self.ok(self.call("GET", f"/runs/{run['id']}", user=viewer))["name"], "By the preparer")
        review = self.call("GET", f"/runs/{run['id']}/review", user=viewer)
        self.assertTrue(self.ok(review)["ready"])
        self.failure(self.call("PATCH", f"/runs/{run['id']}", {"name": "Mine now"}, merge=True, user=viewer,
                               if_match=review.headers["etag"]), 403, "forbidden")
        self.failure(self.call("POST", f"/runs/{run['id']}/submit", user=viewer, idempotency_key=new_key(),
                               if_match=review.headers["etag"]), 403, "forbidden")
        self.assertEqual(self.ok(self.call("GET", f"/runs/{run['id']}", user=viewer))["status"], "draft")

        # A reviewer can do what a preparer can; neither administers.
        self.ok(self.call("PATCH", f"/projects/{project['id']}", {"description": "Reviewed"}, merge=True, user=reviewer,
                          if_match=self.call("GET", f"/projects/{project['id']}", user=reviewer).headers["etag"]))
        self.ok(self.call("POST", f"/runs/{run['id']}/submit", user=preparer, idempotency_key=new_key(),
                          if_match=review.headers["etag"]), 201)
        contract = self.ok(self.call("GET", f"/runs/{run['id']}", user=viewer))["currentContract"]["contractId"]
        for name in (preparer, reviewer):
            refused = self.failure(self.call("POST", f"/operations/deliveries/{contract}/retry", user=name), 403, "forbidden")
            self.assertIn("administer users and operations", refused["message"])
        # Cancelling is preparing too: the viewer cannot, the preparer can.
        etag = self.call("GET", f"/runs/{run['id']}", user=viewer).headers["etag"]
        self.failure(self.call("POST", f"/runs/{run['id']}/cancel", user=viewer, if_match=etag), 403, "forbidden")
        self.assertEqual(self.ok(self.call("POST", f"/runs/{run['id']}/cancel", user=preparer, if_match=etag))["status"], "failed")

    def test_every_operation_that_needs_a_permission_refuses_a_caller_without_it(self):
        import re

        from gea_api import services

        spec = yaml.safe_load((ROOT / "gea/api/openapi.yaml").read_text(encoding="utf-8"))
        needed = {op["operationId"]: (method.upper(), path, op.get("x-permission"))
                  for path, item in spec["paths"].items() for method, op in item.items()
                  if method in ("get", "post", "patch", "delete")}
        declared = {name: permission for name, (_, _, permission) in needed.items() if permission}
        camel = lambda name: re.sub(r"_(\w)", lambda m: m.group(1).upper(), name)
        self.assertEqual({camel(name): permission for name, permission in services.PERMISSIONS.items()}, declared,
                         "the services and the contract name the same permission per operation")
        self.assertEqual(set(declared.values()), {"prepare", "administer"})

        # Nothing but the role decides: a made-up id, an empty body and a stale ETag are still a 403.
        callers = {"prepare": [self.person("viewer")[0]],
                   "administer": [self.person(role)[0] for role in ("viewer", "preparer", "reviewer")]}
        for operation, (method, path, permission) in sorted(needed.items()):
            if not permission:
                continue
            url = re.sub(r"\{version\}", "1", re.sub(r"\{\w+Id\}", str(uuid.uuid4()), path))
            for caller in callers[permission]:
                with self.subTest(operation=operation, caller=caller.split("-")[0]):
                    response = self.call(method, url, None if method in ("GET", "DELETE") else {}, merge=method == "PATCH",
                                         user=caller, idempotency_key=new_key(), if_match='"stale"')
                    self.failure(response, 403, "forbidden")

        # Reading needs no permission: every list is open to a viewer.
        viewer = callers["prepare"][0]
        for path in ("/projects", "/runs", "/jobs", "/users", "/roles", "/regions", "/business-purposes", "/benefits",
                     "/treaties", "/datasets", "/users/me"):
            with self.subTest(path=path):
                self.ok(self.call("GET", path, user=viewer))

    def test_an_admin_gives_a_user_a_role(self):
        admin, me = self.person("admin")
        other, target = self.person("viewer")
        self.assertEqual(target["role"], "viewer")

        listed = self.ok(self.call("GET", f"/users?role=viewer&q={other}", user=other))["users"]       # a viewer may read the list
        self.assertEqual(listed, [{"id": target["id"], "name": other, "region": "global", "regionName": "Global",
                                   "role": "viewer", "roleName": "Viewer"}])
        self.assertEqual(self.ok(self.call("GET", f"/users?role=admin&q={other}", user=admin))["users"], [])
        page = self.ok(self.call("GET", "/users?limit=2", user=admin))
        self.assertEqual(len(page["users"]), 2)
        self.assertTrue(page["nextCursor"])
        following = self.ok(self.call("GET", f"/users?limit=2&cursor={page['nextCursor']}", user=admin))["users"]
        self.assertFalse({u["id"] for u in following} & {u["id"] for u in page["users"]})
        system = self.sql('SELECT "Id"::text AS id FROM gea."User" WHERE "Subject" = \'system\'')[0]["id"]
        self.assertNotIn(system, [u["id"] for u in self.ok(self.call("GET", "/users?q=System&limit=100", user=admin))["users"]],
                         "the seeded system user is not a person")

        path = f"/users/{target['id']}"
        self.failure(self.call("GET", path, user=other), 403, "forbidden")                        # not even their own row
        reading = self.call("GET", path, user=admin)
        self.assertEqual(self.ok(reading), target)
        etag = reading.headers["etag"]
        self.assertEqual(self.call("GET", path, user=admin, if_none_match=etag).status_code, 304)
        self.failure(self.call("PATCH", path, {"role": "preparer"}, merge=True, user=admin), 428, "precondition_required")
        self.failure(self.call("PATCH", path, {"role": "preparer"}, merge=True, user=admin, if_match='"old"'), 412, "precondition_failed")
        invalid = self.failure(self.call("PATCH", path, {"role": "owner", "region": "atlantis", "name": "x"}, merge=True,
                                         user=admin, if_match=etag), 422, "validation_failed")
        self.assertEqual([(i["pointer"], i["code"]) for i in invalid["issues"]], [("/name", "unknown_field")])
        invalid = self.failure(self.call("PATCH", path, {"role": "owner", "region": "atlantis"}, merge=True,
                                         user=admin, if_match=etag), 422, "validation_failed")
        self.assertEqual([(i["pointer"], i["code"]) for i in invalid["issues"]],
                         [("/role", "unknown_option"), ("/region", "unknown_option")])
        self.failure(self.call("PATCH", path, {}, merge=True, user=admin, if_match=etag), 422, "validation_failed")

        body = {"name": f"Raised {uuid.uuid4().hex[:8]}", "region": "europe", "businessPurpose": "pricing", "benefit": ["mortality"]}
        self.failure(self.call("POST", "/projects", body, user=other, idempotency_key=new_key()), 403, "forbidden")
        changing = self.call("PATCH", path, {"role": "preparer", "region": "asia"}, merge=True, user=admin, if_match=etag)
        changed = self.ok(changing)
        self.assertEqual((changed["role"], changed["roleName"], changed["region"], changed["permissions"]),
                         ("preparer", "Preparer", "asia", {"prepare": True, "review": False, "administer": False}))
        self.assertNotEqual(changing.headers["etag"], etag)
        self.ok(self.call("POST", "/projects", body, user=other, idempotency_key=new_key()), 201)    # at once, no new sign-in
        events = self.sql('SELECT "EventType" AS event, "Payload" AS payload, "ActorId"::text AS actor FROM gea."AuditEvent" '
                          'WHERE "AggregateType" = \'User\' AND "AggregateId" = %(id)s::uuid ORDER BY "Id"', id=target["id"])
        self.assertEqual([(e["event"], e["payload"], e["actor"]) for e in events],
                         [("user.role_changed", {"from": "viewer", "to": "preparer"}, me["id"]),
                          ("user.region_changed", {"from": "global", "to": "asia"}, me["id"])])
        # Sending what is already there changes nothing and records nothing.
        same = self.call("PATCH", path, {"role": "preparer"}, merge=True, user=admin, if_match=changing.headers["etag"])
        self.assertEqual(same.headers["etag"], changing.headers["etag"])
        self.assertEqual(len(self.sql('SELECT 1 AS n FROM gea."AuditEvent" WHERE "AggregateId" = %(id)s::uuid', id=target["id"])), 2)

        # Taking the role away works at once as well.
        self.ok(self.call("PATCH", path, {"role": "viewer"}, merge=True, user=admin, if_match=same.headers["etag"]))
        self.failure(self.call("POST", "/projects", {**body, "name": body["name"] + " 2"}, user=other,
                               idempotency_key=new_key()), 403, "forbidden")

        # Nobody changes their own role; their own home region is theirs to change.
        mine = self.call("GET", f"/users/{me['id']}", user=admin)
        own = self.failure(self.call("PATCH", f"/users/{me['id']}", {"role": "viewer"}, merge=True, user=admin,
                                     if_match=mine.headers["etag"]), 403, "forbidden")
        self.assertEqual(own["message"], "You cannot change your own role. Ask another administrator.")
        self.assertEqual(self.ok(self.call("PATCH", f"/users/{me['id']}", {"region": "europe"}, merge=True, user=admin,
                                           if_match=mine.headers["etag"]))["region"], "europe")
        self.failure(self.call("GET", f"/users/{uuid.uuid4()}", user=admin), 404, "not_found")
        self.failure(self.call("GET", f"/users/{system}", user=admin), 404, "not_found")
        self.failure(self.call("PATCH", f"/users/{system}", {"role": "admin"}, merge=True, user=admin, if_match='"x"'), 404, "not_found")

    # ------------------------------------------------------------------ projects
    def test_create_project_validation_lists_every_field(self):
        body = self.failure(self.call("POST", "/projects", {"name": "", "region": "europe", "benefit": []},
                                      idempotency_key=new_key()), 422, "validation_failed")
        self.assertEqual({(i["pointer"], i["code"]) for i in body["issues"]},
                         {("/name", "required"), ("/businessPurpose", "required"), ("/benefit", "empty")})
        self.assertEqual(body["message"], "3 fields are not valid.")
        unknown = self.failure(self.call("POST", "/projects", {
            "name": "P", "region": "atlantis", "businessPurpose": "pricing", "benefit": ["mortality", "immortality"],
            "state": "signed-off"}, idempotency_key=new_key()), 422, "validation_failed")
        self.assertEqual({(i["pointer"], i["code"]) for i in unknown["issues"]}, {("/state", "unknown_option")})
        unknown = self.failure(self.call("POST", "/projects", {
            "name": "P", "region": "atlantis", "businessPurpose": "pricing", "benefit": ["mortality", "immortality"]},
            idempotency_key=new_key()), 422, "validation_failed")
        self.assertEqual({(i["pointer"], i["code"]) for i in unknown["issues"]},
                         {("/region", "unknown_option"), ("/benefit/1", "unknown_option")})

    def test_create_project_is_idempotent(self):
        key = new_key()
        request = {"name": "Idempotent", "region": "asia", "businessPurpose": "rnd", "benefit": ["longevity", "mortality"],
                   "cycle": "2026"}
        first = self.call("POST", "/projects", request, idempotency_key=key)
        project = self.ok(first, 201)
        self.assertEqual(first.headers["location"], f"/gea/v1/projects/{project['id']}")
        # The members of workbook sheet '04. API Data' (get_project_list), plus benefit and cycle.
        self.assertEqual(set(project), {
            "id", "name", "businessPurpose", "region", "benefit", "cycle", "periodFrom", "periodTo", "state",
            "description", "locked", "inheritedFrom", "runs", "jobs", "analyses", "portfolioAe", "owner",
            "createdAt", "updatedAt", "signedOffAt", "revision"})
        self.assertEqual(project["benefit"], ["mortality", "longevity"])                      # lookup order
        self.assertEqual((project["state"], project["locked"], project["cycle"]), ("in-progress", False, "2026"))
        self.assertEqual(project["owner"]["name"], "tester")
        self.assertEqual(project["runs"], {"count": 0, "completed": 0, "active": 0, "failed": 0, "draft": 0,
                                           "unresolvedFailed": 0})
        self.assertEqual((project["jobs"], project["analyses"], project["portfolioAe"]), ({"count": 0}, {"count": 0}, None))
        self.assertIsNone(project["description"])                                             # empty = null, always present
        self.assertIsNone(project["inheritedFrom"])
        self.assertIsNone(project["signedOffAt"])

        again = self.call("POST", "/projects", request, idempotency_key=key)
        self.assertEqual(self.ok(again, 201), project)
        self.assertEqual(again.headers["etag"], first.headers["etag"])
        self.assertEqual(again.headers["location"], first.headers["location"])

        self.failure(self.call("POST", "/projects", {**request, "name": "Other"}, idempotency_key=key),
                     422, "idempotency_key_reused")
        # The key belongs to the caller: another user may use the same value.
        self.assertNotEqual(self.ok(self.call("POST", "/projects", request, idempotency_key=key, user="someone-else"),
                                    201)["id"], project["id"])
        row = self.sql('SELECT "RegionId" AS region, "BusinessPurposeId" AS purpose, "Period" AS period, "State" AS state '
                       'FROM gea."Project" WHERE "Id" = %(id)s::uuid', id=project["id"])[0]
        self.assertEqual((row["region"], row["purpose"], row["period"], row["state"]), ("asia", "rnd", "2026", "in-progress"))

    def test_a_lost_race_on_the_idempotency_key_replays_the_first_answer(self):
        from gea_api import services

        key, marker = new_key(), uuid.uuid4().hex[:10]
        request = {"name": f"Raced {marker}", "region": "asia", "businessPurpose": "rnd", "benefit": ["mortality"]}
        first = self.ok(self.call("POST", "/projects", request, idempotency_key=key), 201)
        real, misses = services._body, []

        def blind_once(call, sql, **params):     # the second request looks before the first one has committed
            if sql is services.queries.FIND_RECEIPT and not misses:
                misses.append(1)
                return None
            return real(call, sql, **params)

        services._body = blind_once
        try:
            second = self.ok(self.call("POST", "/projects", request, idempotency_key=key), 201)
        finally:
            services._body = real
        self.assertEqual((second, misses), (first, [1]))
        self.assertEqual(len(self.ok(self.call("GET", f"/projects?q={marker}"))["projects"]), 1)  # not created twice

    def test_conditional_read_and_update(self):
        project, etag = self.new_project(description="first")
        path = f"/projects/{project['id']}"
        read = self.call("GET", path)
        self.assertEqual(read.headers["etag"], etag)
        not_modified = self.call("GET", path, if_none_match=etag)
        self.assertEqual((not_modified.status_code, not_modified.content), (304, b""))

        self.failure(self.call("PATCH", path, {"name": "Renamed"}, merge=True), 428, "precondition_required")
        self.failure(self.call("PATCH", path, {"name": "Renamed"}, merge=True, if_match='"stale"'),
                     412, "precondition_failed")
        self.failure(self.call("PATCH", path, {"region": "asia"}, merge=True, if_match=etag), 422, "validation_failed")
        self.failure(self.call("PATCH", path, {"periodFrom": "2026-06-01", "periodTo": "2026-01-01"}, merge=True,
                               if_match=etag), 422, "validation_failed")

        updated = self.call("PATCH", path, {"name": "Renamed", "description": None, "benefit": ["morbidity"]},
                            merge=True, if_match=etag)
        body = self.ok(updated)
        self.assertEqual((body["name"], body["benefit"], body["revision"]), ("Renamed", ["morbidity"], 2))
        self.assertIsNone(body["description"])                                                # null cleared it
        self.assertNotEqual(updated.headers["etag"], etag)
        self.failure(self.call("PATCH", path, {"name": "Again"}, merge=True, if_match=etag), 412, "precondition_failed")
        self.assertEqual(self.call("GET", path, if_none_match=etag).status_code, 200)

    def test_list_projects_pages_with_a_cursor(self):
        marker = uuid.uuid4().hex[:10]
        created = [self.new_project(name=f"{marker} {n}")[0]["id"] for n in range(3)]
        first = self.ok(self.call("GET", f"/projects?q={marker}&limit=2"))
        self.assertEqual([p["id"] for p in first["projects"]], created[:0:-1])                # newest first
        second = self.ok(self.call("GET", f"/projects?q={marker}&limit=2&cursor={first['nextCursor']}"))
        self.assertEqual([p["id"] for p in second["projects"]], created[:1])
        self.assertIsNone(second["nextCursor"])
        self.assertEqual(self.ok(self.call("GET", "/projects?q=100%25_literal"))["projects"], [])

    # ------------------------------------------------------------------ the wizard
    def test_wizard_saves_the_run_as_it_is_filled_in(self):
        project, _ = self.new_project(region="north-america")
        run, etag = self.new_run(project["id"])
        path = f"/runs/{run['id']}"
        self.assertEqual((run["status"], run["locked"], run["region"], run["ready"]), ("draft", False, "north-america", False))
        # Every createRun prop of the workbook is a member of the run; an empty one is null.
        self.assertTrue(set(RUN_PROPS) <= set(run))
        self.assertTrue(all(run[prop] is None for prop in RUN_PROPS if prop not in ("name", "treaty")))
        self.assertEqual([s["key"] for s in run["steps"]], ["main", *STEPS])
        # main (name, treaty) is filled in; assigningExpected has no required prop.
        self.assertEqual({s["key"] for s in run["steps"] if s["status"] == "complete"}, {"main", "assigningExpected"})
        required = [f["prop"] for f in SPEC["createRun"] if f["required"] and f["step"] != "main"]
        self.assertEqual([i["field"] for i in run["issues"]], required)                       # workbook order
        self.assertTrue(all(i["code"] == "required" and i["stepKey"] and i["message"] for i in run["issues"]))

        # Autosave: a partly filled run is stored and answered with 200; it is just not ready yet.
        saved = self.call("PATCH", path, {"dataScope": ["Policy_v1"]}, merge=True, if_match=etag)
        run = self.ok(saved)
        self.assertEqual((run["dataScope"], run["revision"], run["ready"]), (["Policy_v1"], 2, False))
        step = run["steps"][1]
        self.assertEqual((step["key"], step["status"], step["missing"]),
                         ("dataAndSetUp", "incomplete", ["studyPeriod", "studyPeriodTreatyOverride", "investigation"]))

        # The form still holds the old ETag: the save is refused and nothing is overwritten.
        self.failure(self.call("PATCH", path, {"investigation": "mortality"}, merge=True, if_match=etag),
                     412, "precondition_failed")
        self.failure(self.call("PATCH", path, {"investigation": "mortality"}, merge=True), 428, "precondition_required")

        # A merge patch keeps what is already saved; null empties a prop.
        etag = saved.headers["etag"]
        merged = self.call("PATCH", path, {"investigation": "mortality", "studyPeriodTreatyOverride": False,
                                           "studyPeriod": {"start": "2016-03-31", "end": "2026-03-31"},
                                           "studyPeriodExclusions": [{"start": "2019-03-31", "end": "2020-03-31"},
                                                                     {"start": "2021-01-01", "end": "2021-06-30"}]},
                           merge=True, if_match=etag)
        run = self.ok(merged)
        self.assertEqual((run["dataScope"], run["investigation"], run["studyPeriodTreatyOverride"]),
                         (["Policy_v1"], "mortality", False))
        self.assertEqual(run["studyPeriod"], {"start": "2016-03-31", "end": "2026-03-31"})
        self.assertEqual([x["start"] for x in run["studyPeriodExclusions"]], ["2019-03-31", "2021-01-01"])
        self.assertEqual(run["steps"][1]["status"], "complete")
        removed = self.call("PATCH", path, {"investigation": None, "studyPeriodExclusions": []}, merge=True,
                            if_match=merged.headers["etag"])
        run = self.ok(removed)
        self.assertEqual((run["investigation"], run["studyPeriodExclusions"]), (None, None))

        # Wrong input is refused with one issue per prop, and nothing is stored.
        invalid = self.failure(self.call("PATCH", path, {
            "colour": "red", "studyPeriod": {"start": "2026-01-01"}, "eventMonthFilter": "yes", "outputFrequency": "annual",
            "ibnrStudyPeriod": {"start": "2026-03-31", "end": "2016-03-31"}, "amountBasis": "", "name": None},
            merge=True, if_match=removed.headers["etag"]), 422, "validation_failed")
        self.assertEqual({(i["pointer"], i["code"]) for i in invalid["issues"]}, {
            ("/colour", "unknown_field"), ("/studyPeriod", "invalid_type"), ("/eventMonthFilter", "invalid_type"),
            ("/outputFrequency", "invalid_type"), ("/ibnrStudyPeriod", "invalid_type"), ("/amountBasis", "required"),
            ("/name", "required")})
        self.assertEqual({i["stepKey"] for i in invalid["issues"] if i["field"] == "eventMonthFilter"}, {"ibnr"})
        self.assertEqual(self.call("GET", path).headers["etag"], removed.headers["etag"])
        self.failure(self.call("PATCH", path, ["not", "an", "object"], merge=True, if_match="*"), 400, "bad_request")
        self.failure(self.call("PATCH", path, {}, merge=True, if_match=removed.headers["etag"]), 422, "validation_failed")

        # The one condition of the modelling sheet: the mapping is required once the override is on.
        conditional = self.ok(self.call("PATCH", path, {"studyPeriodTreatyOverride": True, "investigation": "mortality"},
                                        merge=True, if_match=removed.headers["etag"]))
        self.assertEqual(conditional["steps"][1]["missing"], ["perTreatyEndDatesMapping"])

        listed = self.ok(self.call("GET", f"/runs?projectId={project['id']}"))["runs"]
        self.assertEqual([(r["name"], r["steps"], r["completeSteps"], r["ready"]) for r in listed],
                         [("Mortality study", 8, 2, False)])
        self.assertEqual(self.call("GET", path, if_none_match=etag).status_code, 200)        # the ETag moved on

    def test_every_workbook_prop_is_a_column_of_the_run(self):
        """What is sent as createRun.<prop> is stored in gea."Run"."<Prop>", typed."""
        run_id = self.complete_run()
        run = self.ok(self.call("GET", f"/runs/{run_id}"))
        self.assertEqual({prop: run[prop] for prop in CONFIG}, CONFIG)
        self.assertTrue(run["ready"])
        row = self.sql('SELECT to_jsonb(r) AS row FROM gea."Run" r WHERE r."Id" = %(id)s::uuid', id=run_id)[0]["row"]
        types = {r["column_name"]: r["data_type"] for r in self.sql(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'gea' AND table_name = 'Run'")}
        expected_type = {"string": "text", "boolean": "boolean", "array<string>": "ARRAY"}
        for field in SPEC["createRun"]:
            value = run[field["prop"]]
            if "columns" in field:                                   # a date range is two date columns
                start, end = field["columns"]
                self.assertEqual((types[start], types[end]), ("date", "date"), field["prop"])
                self.assertEqual({"start": row[start], "end": row[end]}, value, field["prop"])
            elif "column" in field:
                self.assertEqual(types[field["column"]], expected_type[field["type"]], field["prop"])
                self.assertEqual(row[field["column"]], value, field["prop"])
            else:                                                    # the list of ranges is a table
                rows = self.sql('SELECT "StartDate"::text AS s, "EndDate"::text AS e FROM gea."RunStudyPeriodExclusion" '
                                'WHERE "RunId" = %(id)s::uuid ORDER BY "Ordinal"', id=run_id)
                self.assertEqual([{"start": r["s"], "end": r["e"]} for r in rows], value)
        # Nothing else is configuration: the remaining columns are identity and lifecycle.
        columns = {c for f in SPEC["createRun"] for c in f.get("columns", [f.get("column")]) if c}
        self.assertEqual(set(types) - columns, {
            "Id", "ProjectId", "Status", "Locked", "CloneSourceId", "CurrentContractVersion", "SubmittedAt",
            "SubmittedBy", "FailureMessage", "Revision", "CreatedBy", "CreatedAt", "UpdatedBy", "UpdatedAt",
            "JobId", "JobOrdinal", "ResolutionAction", "ResolutionNote", "ResolvedBy", "ResolvedAt",
            "CancelRequestedAt", "CancelRequestedBy"})

    def test_review_submit_and_lock(self):
        project, _ = self.new_project()
        run, etag = self.new_run(project["id"])
        run_id = run["id"]
        review = self.ok(self.call("GET", f"/runs/{run_id}/review"))
        self.assertFalse(review["ready"])
        self.assertEqual({i["stepKey"] for i in review["issues"]}, set(STEPS) - {"assigningExpected"})
        not_ready = self.failure(self.submit(run_id), 422, "not_ready")
        self.assertEqual(not_ready["issues"], review["issues"])
        self.assertTrue(all(i["stepKey"] and i["field"] and i["message"] for i in not_ready["issues"]))

        self.ok(self.call("PATCH", f"/runs/{run_id}", CONFIG, merge=True, if_match=etag))
        reviewed = self.call("GET", f"/runs/{run_id}/review")
        review = self.ok(reviewed)
        self.assertEqual((review["ready"], review["issues"], review["nextContractVersion"]), (True, [], 1))
        self.assertNotIn("contractId", review["preview"])
        self.assertEqual([s["key"] for s in review["preview"]["steps"]], list(STEPS))

        # Another tab changes a prop after the review: the submit is refused, the user reviews again.
        self.ok(self.call("PATCH", f"/runs/{run_id}", {"outputFrequency": ["annual"]}, merge=True,
                          if_match=reviewed.headers["etag"]))
        key = new_key()
        self.failure(self.call("POST", f"/runs/{run_id}/submit", idempotency_key=key,
                               if_match=reviewed.headers["etag"]), 412, "precondition_failed")
        self.failure(self.call("POST", f"/runs/{run_id}/submit", idempotency_key=key), 428, "precondition_required")

        published = self.submit(run_id, key)
        contract = self.ok(published, 201)
        self.assertEqual(published.headers["location"], f"/gea/v1/contracts/{contract['contractId']}")
        document = contract["document"]
        self.assertEqual((contract["version"], document["runId"], document["projectId"]), (1, run_id, project["id"]))
        self.assertEqual(document["steps"][-1]["config"]["outputFrequency"], ["annual"])
        self.assertEqual([s["config"] for s in document["steps"][:-1]], list(STEPS.values())[:-1])
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), contract["contentHash"])
        self.assertEqual(contract["deliveries"], [{"target": "snowflake", "status": "pending", "attempts": 0,
                                                   "lastError": None, "deliveredAt": None,
                                                   "nextAttemptAt": contract["deliveries"][0]["nextAttemptAt"]}])
        schema = yaml.safe_load((ROOT / "gea/spec/data-contract.schema.json").read_text(encoding="utf-8"))
        try:
            import jsonschema
        except ImportError:
            jsonschema = None
        if jsonschema is not None:
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(document)

        # A repeated submit (same key) is the same contract, not version 2.
        self.assertEqual(self.ok(self.submit(run_id, key), 201)["contractId"], contract["contractId"])
        self.assertEqual(len(self.ok(self.call("GET", f"/runs/{run_id}/contracts"))["contracts"]), 1)
        self.assertEqual(self.ok(self.call("GET", f"/contracts/{contract['contractId']}")), contract)
        self.assertEqual(self.ok(self.call("GET", f"/runs/{run_id}/contracts/1")), contract)
        self.failure(self.call("GET", f"/runs/{run_id}/contracts/2"), 404, "not_found")

        # From here the run is locked: every write answers 409 locked.
        locked = self.call("GET", f"/runs/{run_id}")
        run = self.ok(locked)
        self.assertEqual((run["status"], run["locked"], run["ready"], run["currentContract"]["version"]),
                         ("queued", True, False, 1))
        self.assertEqual(run["submittedBy"]["name"], "tester")
        self.assertEqual((run["jobId"], run["resolution"], run["cancelRequestedAt"]), (None, None, None))
        self.failure(self.call("PATCH", f"/runs/{run_id}", {"amountBasis": "x"}, merge=True,
                               if_match=locked.headers["etag"]), 409, "locked")
        self.failure(self.call("PATCH", f"/runs/{run_id}", {"name": "Late"}, merge=True,
                               if_match=locked.headers["etag"]), 409, "locked")
        self.failure(self.call("DELETE", f"/runs/{run_id}", if_match=locked.headers["etag"]), 409, "locked")
        self.failure(self.submit(run_id), 409, "locked")
        self.failure(self.call("POST", f"/runs/{run_id}/resolution", {"action": "mark-resolved"},
                               if_match=locked.headers["etag"]), 409, "invalid_state")
        self.failure(self.call("POST", f"/operations/deliveries/{contract['contractId']}/retry"), 409, "invalid_state")
        self.failure(self.call("POST", f"/runs/{run_id}/contracts", idempotency_key=new_key(),
                               if_match=locked.headers["etag"]), 405, "bad_request")       # submit is /submit now
        counters = self.ok(self.call("GET", f"/projects/{project['id']}"))["runs"]
        self.assertEqual(counters, {"count": 1, "completed": 0, "active": 1, "failed": 0, "draft": 0,
                                    "unresolvedFailed": 0})

    # ------------------------------------------------------------------ execution: observe and decide
    def test_execution_is_observable_from_submit_to_the_last_step(self):
        project, _ = self.new_project()
        draft, _ = self.new_run(project["id"])
        never = self.failure(self.call("GET", f"/runs/{draft['id']}/execution"), 404, "not_found")
        self.assertIn("not been submitted", never["message"])

        run_id = self.complete_run()
        contract = self.ok(self.submit(run_id), 201)
        # Before the contract has reached Snowflake: queued, and `delivery` says where it is.
        waiting = self.call("GET", f"/runs/{run_id}/execution")
        execution = self.ok(waiting)
        self.assertEqual(set(execution), {"contractId", "contractVersion", "status", "delivery", "startedAt", "finishedAt",
                                          "failureMessage", "lastReportedAt", "cancelRequestedAt", "cancelRequestedBy", "steps"})
        self.assertEqual((execution["contractId"], execution["contractVersion"], execution["status"]),
                         (contract["contractId"], 1, "queued"))
        self.assertEqual((execution["delivery"]["status"], execution["lastReportedAt"]), ("pending", None))
        self.assertEqual([(s["stepKey"], s["status"]) for s in execution["steps"]], [(key, "pending") for key in STEPS])
        self.assertEqual(self.call("GET", f"/runs/{run_id}/execution", if_none_match=waiting.headers["etag"]).status_code, 304)

        # Snowflake reports step by step; every report is also the heartbeat.
        self.relay(run_id, "running", [
            {"key": "dataAndSetUp", "status": "complete", "startedAt": "2026-10-02T08:00:00Z",
             "finishedAt": "2026-10-02T08:02:00Z", "detail": {"rows": 1204331, "queryId": "01b2-c3"}},
            {"key": "segmentation", "status": "running", "startedAt": "2026-10-02T08:02:00Z"}])
        running = self.call("GET", f"/runs/{run_id}/execution", if_none_match=waiting.headers["etag"])
        execution = self.ok(running)                                                           # changed: 200, not 304
        self.assertEqual((execution["status"], execution["delivery"]["status"]), ("running", "delivered"))
        self.assertTrue(execution["startedAt"] and execution["lastReportedAt"])
        first, second, third = execution["steps"][:3]
        self.assertEqual((first["status"], first["detail"], first["finishedAt"][:16]),
                         ("complete", {"rows": 1204331, "queryId": "01b2-c3"}, "2026-10-02T08:02"))
        self.assertEqual((second["status"], second["finishedAt"], third["status"]), ("running", None, "pending"))
        self.assertEqual(self.ok(self.call("GET", f"/runs/{run_id}"))["status"], "running")

        # The log lines Snowflake wrote, copied by the relay; copying twice adds nothing.
        lines = json.dumps([
            {"id": "sf-1", "stepKey": "dataAndSetUp", "level": "info", "message": "Read 1,204,331 policy rows",
             "detail": {"rows": 1204331}, "occurredAt": "2026-10-02T08:01:00Z"},
            {"id": "sf-2", "stepKey": "dataAndSetUp", "level": "warning", "message": "412 rows without an issue date were dropped"},
            {"id": "sf-3", "stepKey": "segmentation", "message": "Segmenting by policy year"}])
        for _ in range(2):
            self.sql('SELECT gea."RecordExecutionLog"(%(id)s::uuid, %(lines)s::jsonb) AS n', id=contract["contractId"], lines=lines)
        logs = self.ok(self.call("GET", f"/runs/{run_id}/logs?limit=2"))
        self.assertEqual([(l["message"][:10], l["level"], l["stepKey"], l["contractVersion"]) for l in logs["logs"]],
                         [("Segmenting", "info", "segmentation", 1), ("412 rows w", "warning", "dataAndSetUp", 1)])
        older = self.ok(self.call("GET", f"/runs/{run_id}/logs?limit=2&cursor={logs['nextCursor']}"))
        self.assertEqual([(l["detail"], l["occurredAt"][:16]) for l in older["logs"]], [({"rows": 1204331}, "2026-10-02T08:01")])
        self.assertIsNone(older["nextCursor"])
        self.failure(self.call("GET", f"/runs/{run_id}/logs?cursor=abc"), 422, "validation_failed")
        self.failure(self.call("GET", f"/runs/{uuid.uuid4()}/logs"), 404, "not_found")

        self.relay(run_id, "complete", [{"key": key, "status": "complete"} for key in STEPS])
        execution = self.ok(self.call("GET", f"/runs/{run_id}/execution"))
        self.assertEqual((execution["status"], {s["status"] for s in execution["steps"]}), ("complete", {"complete"}))
        self.assertTrue(execution["finishedAt"])
        self.assertEqual(self.ok(self.call("GET", f"/runs/{run_id}"))["status"], "complete")

    def test_a_failed_run_is_resolved(self):
        """Run.ResolutionAction: re-run with fixed config, or mark resolved."""
        project, _ = self.new_project()
        run, _ = self.new_run(project["id"], **CONFIG)
        run_id = run["id"]
        contract = self.ok(self.submit(run_id), 201)
        self.relay(run_id, "failed", [{"key": "dataAndSetUp", "status": "complete"},
                                      {"key": "segmentation", "status": "failed", "detail": {"error": "no exposure rows"}}],
                   "Segmentation failed")
        failed = self.call("GET", f"/runs/{run_id}")
        run = self.ok(failed)
        self.assertEqual((run["status"], run["failureMessage"], run["currentContract"]["snowflakeDelivery"], run["resolution"]),
                         ("failed", "Segmentation failed", "delivered", None))
        execution = self.ok(self.call("GET", f"/runs/{run_id}/execution"))
        self.assertEqual((execution["status"], execution["failureMessage"]), ("failed", "Segmentation failed"))
        self.assertEqual([s["status"] for s in execution["steps"]][:3], ["complete", "failed", "pending"])
        self.assertEqual(self.ok(self.call("GET", f"/projects/{project['id']}"))["runs"]["unresolvedFailed"], 1)

        path = f"/runs/{run_id}/resolution"
        self.failure(self.call("POST", path, {"action": "mark-resolved"}), 428, "precondition_required")
        self.failure(self.call("POST", path, {"action": "mark-resolved"}, if_match='"stale"'), 412, "precondition_failed")
        wrong = self.failure(self.call("POST", path, {"action": "ignore", "note": 7, "by": "me"},
                                       if_match=failed.headers["etag"]), 422, "validation_failed")
        self.assertEqual({(i["pointer"], i["code"]) for i in wrong["issues"]},
                         {("/action", "unknown_option"), ("/note", "invalid_type"), ("/by", "unknown_field")})

        # Mark resolved: the run stays failed, with who accepted the failure and why.
        marking = self.call("POST", path, {"action": "mark-resolved", "note": "  Known data gap, accepted  "},
                            if_match=failed.headers["etag"])
        marked = self.ok(marking)
        self.assertEqual((marked["status"], marked["locked"]), ("failed", True))
        resolution = marked["resolution"]
        self.assertEqual((resolution["action"], resolution["note"], resolution["resolvedBy"]["name"]),
                         ("mark-resolved", "Known data gap, accepted", "tester"))
        self.assertTrue(resolution["resolvedAt"])
        counters = self.ok(self.call("GET", f"/projects/{project['id']}"))["runs"]
        self.assertEqual((counters["failed"], counters["unresolvedFailed"]), (1, 0))
        listed = self.ok(self.call("GET", f"/runs?projectId={project['id']}&status=failed"))["runs"]
        self.assertEqual([(r["id"], r["resolutionAction"]) for r in listed], [(run_id, "mark-resolved")])

        # Re-run with fixed config: back to draft, fix, submit again as version 2.
        rerunning = self.call("POST", path, {"action": "rerun-with-fixed-config"}, if_match=marking.headers["etag"])
        reopened = self.ok(rerunning)
        self.assertEqual((reopened["status"], reopened["locked"], reopened["ready"]), ("draft", False, True))
        self.assertEqual((reopened["resolution"]["action"], reopened["resolution"]["note"]), ("rerun-with-fixed-config", None))
        self.failure(self.call("POST", path, {"action": "mark-resolved"}, if_match=rerunning.headers["etag"]), 409, "invalid_state")
        self.ok(self.call("PATCH", f"/runs/{run_id}", {"exposureExclusion": ["none"]}, merge=True,
                          if_match=rerunning.headers["etag"]))
        second = self.ok(self.submit(run_id), 201)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["document"]["steps"][1]["config"]["exposureExclusion"], ["none"])
        self.assertEqual([c["version"] for c in self.ok(self.call("GET", f"/runs/{run_id}/contracts"))["contracts"]], [2, 1])
        self.assertEqual(self.ok(self.call("GET", f"/runs/{run_id}/contracts/1")), self.ok(
            self.call("GET", f"/contracts/{contract['contractId']}")))                       # version 1 is untouched
        requeued = self.ok(self.call("GET", f"/runs/{run_id}"))
        self.assertEqual((requeued["status"], requeued["resolution"], requeued["failureMessage"]), ("queued", None, None))

    def test_cancel_withdraws_or_requests(self):
        # Not sent yet: the contract is withdrawn and the run fails at once.
        run_id = self.complete_run()
        self.ok(self.submit(run_id), 201)
        queued = self.call("GET", f"/runs/{run_id}")
        self.failure(self.call("POST", f"/runs/{run_id}/cancel"), 428, "precondition_required")
        cancelling = self.call("POST", f"/runs/{run_id}/cancel", if_match=queued.headers["etag"])
        cancelled = self.ok(cancelling)
        self.assertEqual((cancelled["status"], cancelled["failureMessage"], cancelled["currentContract"]["snowflakeDelivery"]),
                         ("failed", "Cancelled by tester before execution started.", "cancelled"))
        self.assertTrue(cancelled["cancelRequestedAt"])
        execution = self.ok(self.call("GET", f"/runs/{run_id}/execution"))
        self.assertEqual((execution["status"], execution["cancelRequestedBy"]["name"], {s["status"] for s in execution["steps"]}),
                         ("failed", "tester", {"skipped"}))
        self.assertEqual(self.ok(self.call("GET", f"/runs/{run_id}/logs"))["logs"][0]["level"], "warning")
        self.failure(self.call("POST", f"/runs/{run_id}/cancel", if_match=cancelling.headers["etag"]), 409, "invalid_state")
        self.failure(self.call("POST", f"/operations/deliveries/{cancelled['currentContract']['contractId']}/retry"),
                     409, "invalid_state")
        # A cancelled run is a failed run: it is resolved like any other failure.
        rerun = self.ok(self.call("POST", f"/runs/{run_id}/resolution", {"action": "rerun-with-fixed-config"},
                                  if_match=cancelling.headers["etag"]))
        self.assertEqual((rerun["status"], rerun["cancelRequestedAt"]), ("draft", None))

        # Under way: a request the relay passes on; the run keeps running until Snowflake reports.
        other = self.complete_run()
        self.ok(self.submit(other), 201)
        contract = self.relay(other, "running")
        running = self.call("GET", f"/runs/{other}")
        requesting = self.call("POST", f"/runs/{other}/cancel", if_match=running.headers["etag"])
        requested = self.ok(requesting)
        self.assertEqual(requested["status"], "running")
        self.assertTrue(requested["cancelRequestedAt"])
        again = self.ok(self.call("POST", f"/runs/{other}/cancel", if_match=requesting.headers["etag"]))   # harmless
        self.assertEqual(again["cancelRequestedAt"], requested["cancelRequestedAt"])
        pending = self.sql('SELECT "ContractId"::text AS id FROM gea."RunCancelRequest" WHERE "RunId" = %(id)s::uuid', id=other)
        self.assertEqual([row["id"] for row in pending], [contract])                          # what the relay reads
        self.sql('SELECT gea."RecordExecutionStatus"(%(id)s::uuid, \'failed\', \'[]\'::jsonb, \'Cancelled by tester.\') AS s',
                 id=contract)
        stopped = self.ok(self.call("GET", f"/runs/{other}"))
        self.assertEqual((stopped["status"], stopped["failureMessage"]), ("failed", "Cancelled by tester."))

        draft, etag = self.new_run(self.new_project()[0]["id"])
        self.failure(self.call("POST", f"/runs/{draft['id']}/cancel", if_match=etag), 409, "invalid_state")

    # ------------------------------------------------------------------ jobs
    def test_a_job_submits_several_runs_in_order(self):
        project, _ = self.new_project()
        runs = [self.new_run(project["id"], name=f"Batch {n}", **CONFIG)[0]["id"] for n in (1, 2, 3)]
        incomplete, _ = self.new_run(project["id"], name="Half done", dataScope=["Policy_v1"])
        submitted = self.complete_run()
        self.ok(self.submit(submitted), 201)

        self.failure(self.call("POST", "/jobs", {"name": "Batch", "runIds": runs}), 400, "bad_request")   # no Idempotency-Key
        shape = self.failure(self.call("POST", "/jobs", {"name": " ", "runIds": [runs[0]], "maxParallel": 0, "kind": "basis"},
                                       idempotency_key=new_key()), 422, "validation_failed")
        self.assertEqual({(i["pointer"], i["code"]) for i in shape["issues"]},
                         {("/name", "required"), ("/runIds", "invalid"), ("/maxParallel", "invalid"), ("/kind", "unknown_field")})
        unknown = self.failure(self.call("POST", "/jobs", {"name": "Batch", "runIds": [runs[0], str(uuid.uuid4())]},
                                         idempotency_key=new_key()), 422, "validation_failed")
        self.assertEqual([i["pointer"] for i in unknown["issues"]], ["/runIds/1"])
        # A job is all or nothing: one run that is not a complete draft and nothing is submitted.
        refused = self.failure(self.call("POST", "/jobs", {"name": "Batch", "runIds": [runs[0], incomplete["id"], submitted]},
                                         idempotency_key=new_key()), 422, "not_ready")
        self.assertEqual({i["pointer"] for i in refused["issues"]}, {"/runIds/1", "/runIds/2"})
        self.assertEqual([i["code"] for i in refused["issues"] if i["pointer"] == "/runIds/2"], ["not_draft"])
        self.assertTrue(all(i["message"].startswith('Run "Half done": ') for i in refused["issues"] if i["pointer"] == "/runIds/1"))
        self.assertEqual(self.ok(self.call("GET", f"/runs/{runs[0]}"))["status"], "draft")

        key = new_key()
        request = {"name": "Mortality batch", "note": "Q3 refresh", "runIds": [runs[2], runs[0], runs[1]], "maxParallel": 1}
        creating = self.call("POST", "/jobs", request, idempotency_key=key)
        job = self.ok(creating, 201)
        self.assertEqual(creating.headers["location"], f"/gea/v1/jobs/{job['id']}")
        self.assertEqual(set(job), {"id", "name", "kind", "note", "projectId", "projectName", "status", "maxParallel", "runs",
                                    "progressPercentage", "durationSeconds", "finishedAt", "submittedBy", "submittedAt"})
        self.assertEqual((job["name"], job["kind"], job["note"], job["projectId"], job["projectName"], job["maxParallel"]),
                         ("Mortality batch", "data", "Q3 refresh", project["id"], project["name"], 1))
        self.assertEqual((job["status"], job["runs"], job["progressPercentage"], job["durationSeconds"], job["finishedAt"]),
                         ("queued", {"count": 3, "completed": 0, "failed": 0, "running": 0, "queued": 3}, 0, None, None))
        self.assertEqual(self.ok(self.call("POST", "/jobs", request, idempotency_key=key), 201), job)   # not submitted twice
        # Every run has its contract, is queued, and knows its job and its place in it.
        listed = self.ok(self.call("GET", f"/runs?jobId={job['id']}"))["runs"]
        self.assertEqual({r["id"]: (r["status"], r["jobOrdinal"], r["currentContractVersion"]) for r in listed},
                         {runs[2]: ("queued", 1, 1), runs[0]: ("queued", 2, 1), runs[1]: ("queued", 3, 1)})
        self.assertEqual(self.ok(self.call("GET", f"/runs/{runs[0]}"))["jobId"], job["id"])
        self.assertEqual(self.ok(self.call("GET", f"/projects/{project['id']}"))["jobs"], {"count": 1})
        self.failure(self.call("POST", "/jobs", {"name": "Again", "runIds": runs}, idempotency_key=new_key()), 422, "not_ready")

        # maxParallel = 1: the relay is handed one contract of the job at a time, in job order.
        def claim():
            rows = self.sql('SELECT "RunId"::text AS run FROM gea."ClaimContractDeliveries"(\'test-relay\', 1000)')
            return [row["run"] for row in rows if row["run"] in runs]

        def finish(run_id, status, message=None):
            contract = self.ok(self.call("GET", f"/runs/{run_id}"))["currentContract"]["contractId"]
            self.sql('SELECT gea."CompleteContractDelivery"(%(id)s::uuid, \'snowflake\', \'test-relay\', \'q\') AS done', id=contract)
            self.sql('SELECT gea."RecordExecutionStatus"(%(id)s::uuid, %(status)s::text, \'[]\'::jsonb, %(message)s::text) AS s',
                     id=contract, status=status, message=message)

        polled = self.call("GET", f"/jobs/{job['id']}")
        self.assertEqual(self.call("GET", f"/jobs/{job['id']}", if_none_match=polled.headers["etag"]).status_code, 304)
        self.assertEqual(claim(), [runs[2]])
        self.assertEqual(claim(), [])                                                         # the next one waits
        finish(runs[2], "complete")
        moved = self.call("GET", f"/jobs/{job['id']}", if_none_match=polled.headers["etag"])
        progress = self.ok(moved)                                                              # the ETag moved with the run
        self.assertEqual((progress["status"], progress["runs"]["completed"], progress["progressPercentage"]), ("running", 1, 33.3))
        self.assertEqual(claim(), [runs[0]])
        finish(runs[0], "failed", "IBNR failed")                                              # a failure does not stop the job
        self.assertEqual(claim(), [runs[1]])
        finish(runs[1], "complete")
        done = self.ok(self.call("GET", f"/jobs/{job['id']}"))
        self.assertEqual((done["status"], done["runs"], done["progressPercentage"]),
                         ("completed-with-failures", {"count": 3, "completed": 2, "failed": 1, "running": 0, "queued": 0}, 66.7))
        self.assertTrue(done["finishedAt"] and done["durationSeconds"] >= 0)

        jobs = self.ok(self.call("GET", f"/jobs?projectId={project['id']}&status=completed-with-failures"))
        self.assertEqual(([j["id"] for j in jobs["jobs"]], jobs["nextCursor"]), ([job["id"]], None))
        self.assertEqual(self.ok(self.call("GET", f"/jobs?projectId={project['id']}&status=queued"))["jobs"], [])
        self.failure(self.call("GET", "/jobs?status=paused"), 422, "validation_failed")
        self.failure(self.call("GET", f"/jobs/{uuid.uuid4()}"), 404, "not_found")

        # The failed run is fixed and submitted again on its own: it is still part of its job.
        failed = self.call("GET", f"/runs/{runs[0]}")
        rerun = self.call("POST", f"/runs/{runs[0]}/resolution", {"action": "rerun-with-fixed-config"},
                          if_match=failed.headers["etag"])
        self.assertEqual(self.ok(rerun)["jobId"], job["id"])
        self.ok(self.submit(runs[0]), 201)
        self.assertEqual(claim(), [runs[0]])
        finish(runs[0], "complete")
        self.assertEqual(self.ok(self.call("GET", f"/jobs/{job['id']}"))["status"], "complete")

    def test_a_job_without_a_limit_starts_all_its_runs(self):
        first, second = self.new_project()[0], self.new_project()[0]
        runs = [self.new_run(first["id"], **CONFIG)[0]["id"], self.new_run(second["id"], **CONFIG)[0]["id"]]
        job = self.ok(self.call("POST", "/jobs", {"name": "Across projects", "runIds": runs}, idempotency_key=new_key()), 201)
        self.assertEqual((job["maxParallel"], job["projectId"], job["projectName"], job["note"]), (None, None, None, None))
        rows = self.sql('SELECT "RunId"::text AS run FROM gea."ClaimContractDeliveries"(\'test-relay\', 1000)')
        self.assertTrue(set(runs) <= {row["run"] for row in rows})

    def test_clone_and_delete(self):
        source_id = self.complete_run()
        self.ok(self.submit(source_id), 201)
        project, _ = self.new_project()
        clone, etag = self.new_run(project["id"], name="Clone", cloneSourceId=source_id, amountBasis="lives")
        self.assertEqual((clone["status"], clone["cloneSourceId"], clone["projectId"]), ("draft", source_id, project["id"]))
        # The configuration of the locked source is copied; what was sent with the request wins.
        self.assertEqual({prop: clone[prop] for prop in CONFIG}, {**CONFIG, "amountBasis": "lives"})
        self.assertTrue(all(s["status"] == "complete" for s in clone["steps"]))

        missing = self.failure(self.call("POST", "/runs", {"projectId": project["id"], "name": "x", "treaty": "t",
                                                           "cloneSourceId": str(uuid.uuid4())},
                                         idempotency_key=new_key()), 422, "validation_failed")
        self.assertEqual(missing["issues"][0]["pointer"], "/cloneSourceId")
        unknown = self.failure(self.call("POST", "/runs", {"projectId": str(uuid.uuid4()), "name": "x", "treaty": "t"},
                                         idempotency_key=new_key()), 422, "validation_failed")
        self.assertEqual(unknown["issues"][0]["pointer"], "/projectId")
        incomplete = self.failure(self.call("POST", "/runs", {"name": "x"}, idempotency_key=new_key()),
                                  422, "validation_failed")
        self.assertEqual({i["pointer"] for i in incomplete["issues"]}, {"/projectId", "/treaty"})

        self.failure(self.call("DELETE", f"/runs/{clone['id']}"), 428, "precondition_required")
        deleted = self.call("DELETE", f"/runs/{clone['id']}", if_match=etag)
        self.assertEqual((deleted.status_code, deleted.json()), (200, {"data": None, "error": None}))
        self.failure(self.call("GET", f"/runs/{clone['id']}"), 404, "not_found")

    def test_run_parameters_follow_the_project_scope(self):
        """A dropdown value can be limited to a region; a single value is a pre-populated default."""
        value = f"central-{uuid.uuid4().hex[:6]}"
        treaty = f"TRT-{uuid.uuid4().hex[:6]}"
        self.owner_only('INSERT INTO gea."ParameterOption" ("Parameter", "Value", "Label", "SortOrder", "RegionId", "IsDefault") '
                        "VALUES ('exposureMethod', %(value)s::text, 'Central', 2, 'asia', true), "
                        "('treaty', %(treaty)s::text, 'Treaty of Asia', 1, 'asia', false) RETURNING \"Id\" AS id",
                        value=value, treaty=treaty)
        try:
            asia, _ = self.new_project(region="asia")
            europe, _ = self.new_project(region="europe")

            def exposure(project):
                parameters = self.ok(self.call("GET", f"/projects/{project['id']}/run-parameters?investigation=mortality"))
                self.assertEqual(parameters["scope"], {"region": project["region"], "businessPurpose": "pricing",
                                                       "benefit": ["mortality"], "investigation": "mortality"})
                return next(p for p in parameters["parameters"] if p["name"] == "exposureMethod")

            in_asia, in_europe = exposure(asia), exposure(europe)
            self.assertIn({"value": value, "label": "Central"}, in_asia["options"])
            self.assertEqual(in_asia["default"], value)
            self.assertEqual((in_europe["options"], in_europe["default"]), ([{"value": "initial", "label": "Initial"}], "initial"))
            # The treaty list is scoped the same way.
            self.assertIn({"id": treaty, "name": "Treaty of Asia"}, self.ok(self.call("GET", "/treaties?region=asia"))["treaties"])
            self.assertNotIn(treaty, [t["id"] for t in self.ok(self.call("GET", "/treaties?region=europe"))["treaties"]])
            self.assertEqual(self.ok(self.call("GET", f"/treaties?q={treaty[4:]}"))["treaties"], [{"id": treaty, "name": "Treaty of Asia"}])
        finally:
            self.sql('DELETE FROM gea."ParameterOption" WHERE "Value" IN (%(value)s::text, %(treaty)s::text) RETURNING "Id" AS id',
                     value=value, treaty=treaty)


class RouteTableTest(unittest.TestCase):
    """The handlers registered in gea_api.routes are exactly the operations of the OpenAPI contract."""

    def test_routes_match_the_contract(self):
        import re

        try:
            from gea_api import routes
        except ImportError as error:
            self.skipTest(f"gea_api cannot be imported: {error}")
        spec = yaml.safe_load((ROOT / "gea/api/openapi.yaml").read_text(encoding="utf-8"))
        contract = set()
        for path, item in spec["paths"].items():
            generic = re.sub(r"\{\w+\}", "{}", path)
            contract |= {(method.upper(), generic) for method in item if method in ("get", "post", "put", "patch", "delete")}
        served = set()
        for handler in routes.HANDLERS:
            for path in handler.paths:
                for method in handler.http_methods:
                    served.add((str(getattr(method, "value", method)).upper(), re.sub(r"\{\w+:\w+\}", "{}", path)))
        served -= {("GET", "/openapi.yaml"), ("OPTIONS", "/openapi.yaml")}
        served = {(method, path) for method, path in served if method != "OPTIONS"}
        self.assertEqual(served, contract)


if __name__ == "__main__":
    unittest.main()
