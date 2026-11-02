"""Opt-in real MongoDB CRUD smoke test; credentials stay in ignored local fixture."""

from pathlib import Path
import json
import uuid
import pytest
import main

FIXTURE = Path(__file__).with_name("live-mongodb.local.json")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="No local MongoDB fixture")


def test_live_crud_ejson_and_bulk_guard(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    fixture = json.loads(FIXTURE.read_text())
    collection = "fs_extension_test_" + uuid.uuid4().hex
    connection = {
        "connection_id": "live",
        "connection_type": "mongodb",
        "config": {
            "host": fixture["host"],
            "port": fixture.get("port", 27017),
            "database": fixture.get("database", "flowsteward_test"),
            "tls": fixture.get("tls", False),
            "srv": fixture.get("srv", False),
            "auth_source": fixture.get("auth_source", "admin"),
            "request_timeout_seconds": 15,
        },
        "secrets": {
            "username": fixture.get("username", ""),
            "password": fixture.get("password", ""),
        },
    }

    def run(name, **args):
        result = main.handle(
            {
                "contract_version": "extension_host_v2",
                "mode": "action",
                "runtime_context": {
                    "group_id": None,
                    "for_each_id": None,
                    "step_id": "live",
                    "step_execution_id": "live",
                    "account_id": "smoke",
                    "project_id": "mongodb",
                    "external_effect_id": uuid.uuid4().hex,
                },
                "action": {
                    "action_id": name,
                    "input": {"connection_ref": "live", "collection": collection, **args},
                    "target": {"connection": connection},
                },
            }
        )
        assert result["ok"], result
        return result["result"]

    marker = uuid.uuid4().hex
    try:
        first = run(
            "insert_one",
            document={
                "marker": marker,
                "n": {"$numberLong": "9007199254740993"},
                "text": "{{literal}} $HOME",
            },
        )
        assert "$oid" in first["inserted_id"]
        one = run("find_one", filter={"_id": first["inserted_id"]})
        assert one["document"]["n"] == {"$numberLong": "9007199254740993"}
        assert one["document"]["text"] == "{{literal}} $HOME"
        inserted = run(
            "insert_many",
            documents=[{"marker": marker, "order": 2}, {"marker": marker, "order": 3}],
        )
        assert len(inserted["inserted_ids"]) == 2
        found = run("find", filter={"marker": marker}, sort={"order": -1}, limit=2)
        assert found["count"] == 2
        assert [doc["order"] for doc in found["documents"]] == [
            {"$numberInt": "3"},
            {"$numberInt": "2"},
        ]
        projected = run(
            "find", filter={"marker": marker}, projection={"marker": 1, "_id": 0}, limit=1
        )
        assert projected["documents"] == [{"marker": marker}]
        upsert = run(
            "update_one",
            filter={"marker": marker, "upserted": True},
            update={"$set": {"new": True}},
            upsert=True,
        )
        assert upsert["matched_count"] == 0 and upsert["upserted_count"] == 1
        assert "$oid" in upsert["upserted_id"]
        assert run("delete_one", filter={"_id": upsert["upserted_id"]})["deleted_count"] == 1
        assert run("delete_one", filter={"_id": upsert["upserted_id"]})["deleted_count"] == 0
        updated = run(
            "update_one", filter={"_id": first["inserted_id"]}, update={"$set": {"changed": True}}
        )
        assert updated["modified_count"] == 1
        updated = run(
            "update_many",
            filter={"marker": marker},
            update={"$set": {"bulk": True}},
            allow_many=True,
        )
        assert updated["matched_count"] == 3
        aggregate = run("aggregate", pipeline=[{"$match": {"marker": marker}}, {"$count": "total"}])
        assert aggregate["documents"][0]["total"] == {"$numberInt": "3"}
        assert run("delete_one", filter={"_id": first["inserted_id"]})["deleted_count"] == 1
        assert run("find_one", filter={"_id": first["inserted_id"]})["document"] is None
        assert run("delete_many", filter={"marker": marker}, allow_many=True)["deleted_count"] == 2
    finally:
        run("delete_many", filter={"marker": marker}, allow_many=True)


def test_live_wrong_credentials_are_redacted(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    fixture = json.loads(FIXTURE.read_text())
    password = "intentionally-wrong-private-password"
    result = main.handle(
        {
            "contract_version": "extension_host_v2",
            "mode": "action",
            "runtime_context": {
                "group_id": None,
                "for_each_id": None,
                "step_id": "live",
                "step_execution_id": "live",
                "account_id": "smoke",
                "project_id": "mongodb",
            },
            "action": {
                "action_id": "find",
                "input": {"connection_ref": "live", "collection": "items"},
                "target": {
                    "connection": {
                        "connection_id": "live",
                        "connection_type": "mongodb",
                        "config": {
                            "host": fixture["host"],
                            "port": fixture.get("port", 27017),
                            "database": fixture.get("database", "flowsteward_test"),
                            "tls": fixture.get("tls", False),
                            "request_timeout_seconds": 5,
                        },
                        "secrets": {"username": fixture.get("username", ""), "password": password},
                    }
                },
            },
        }
    )
    assert not result["ok"] and result["retryable"] is False
    serialized = json.dumps(result)
    assert password not in serialized and fixture["host"] not in serialized


def test_live_dataset_discovery_scope_and_typed_reads(monkeypatch):
    """Real host admin envelopes and packaged native driver against a restricted user."""
    from copy import deepcopy
    from test_datasets import request

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    fixture = json.loads(FIXTURE.read_text())
    if "reader_username" not in fixture:
        pytest.skip("No restricted-reader Dataset fixture")

    def run(name="preview_query", arguments=None, **extra):
        payload = request(name, arguments)
        args = payload["action"]["input"]
        args["connection_config"] = {
            "host": fixture["host"],
            "database": fixture.get("database", "flowsteward_test"),
            "tls": False,
            "request_timeout_seconds": 15,
        }
        args["secret_refs"] = {
            "values": {
                "username": fixture["reader_username"],
                "password": fixture["reader_password"],
            }
        }
        args.update(extra)
        return main.handle(payload)

    # The reader has collection find privilege but no listCollections database privilege.
    # authorizedCollections+nameOnly must enumerate only its allowed collection.
    # Omit dataset entirely for discovery (null is deliberately not a scope bypass).
    payload = request("introspect_schema")
    args = payload["action"]["input"]
    args.pop("dataset")
    args.pop("query_template")
    args["connection_config"] = {
        "host": fixture["host"],
        "database": fixture.get("database", "flowsteward_test"),
        "tls": False,
    }
    args["secret_refs"] = {
        "values": {"username": fixture["reader_username"], "password": fixture["reader_password"]}
    }
    result = main.handle(payload)
    assert result["ok"], result
    assert {r["resource_id"] for r in result["result"]["resources"]} == {"items"}
    assert result["result"]["resources"][0]["metadata"]["sample_rows"] <= 5
    result = run("test_connection")
    assert result["ok"], result
    result = run()
    assert result["ok"] and result["result"]["rows"] == [
        {"x": False},
        {"x": 0},
        {"x": None},
        {"x": {"a": 1}},
        {"x": [1, 2]},
    ], result
    assert all("_id" not in row for row in result["result"]["rows"])
    for value, kind in [
        (False, "boolean"),
        (0, "integer"),
        (None, "null"),
        ({"a": 1}, "object"),
        ([1, 2], "array"),
    ]:
        template = deepcopy(request()["action"]["input"]["query_template"])
        template["query_definition_jsonb"]["arguments"] = {
            "filter": {"x": {"$eq": {"$param": "v"}}},
            "projection": {"x": 1},
        }
        template["param_schema_jsonb"] = {"properties": {"v": {"type": kind}}}
        result = run(query_template=template, params={"v": value})
        assert result["ok"] and result["result"]["rows"], result
        assert all(row["x"] == value for row in result["result"]["rows"])
    template = deepcopy(request()["action"]["input"]["query_template"])
    template["query_definition_jsonb"].update(
        operation="aggregate",
        arguments={
            "pipeline": [
                {"$match": {"n": {"$gte": 1}}},
                {"$sort": {"n": 1}},
                {"$project": {"n": 1}},
            ]
        },
    )
    result = run(query_template=template, limits={"max_preview_rows": 2})
    assert result["ok"] and result["result"]["rows"] == [{"n": 1}, {"n": 2}], result
    result = run("introspect_schema")
    assert result["ok"], result
    assert {c["name"] for c in result["result"]["resources"][0]["columns"]} == {"x", "n"}
    result = run(arguments={"filter": {}, "projection": {"private": 1}})
    assert not result["ok"] and result["definitely_no_external_effect"]


def test_live_review_discovery_budgets_and_parameter_positions(monkeypatch):
    from copy import deepcopy
    from test_datasets import request

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    fixture = json.loads(FIXTURE.read_text())
    if not fixture.get("review_cases"):
        pytest.skip("Review live fixture must seed system.views and a 121-byte collection")

    def run(name, arguments=None, **extra):
        p = request(name, arguments)
        a = p["action"]["input"]
        a["connection_config"] = {
            "host": fixture["host"],
            "database": fixture.get("database", "flowsteward_test"),
            "tls": False,
        }
        a["secret_refs"] = {
            "values": {"username": fixture["username"], "password": fixture["password"]}
        }
        a.update(extra)
        return main.handle(p)

    p = request("introspect_schema")
    a = p["action"]["input"]
    a.pop("dataset")
    a.pop("query_template")
    a["connection_config"] = {
        "host": fixture["host"],
        "database": fixture.get("database", "flowsteward_test"),
        "tls": False,
    }
    a["secret_refs"] = {
        "values": {"username": fixture["username"], "password": fixture["password"]}
    }
    result = main.handle(p)
    assert result["ok"], result
    names = {r["resource_id"] for r in result["result"]["resources"]}
    assert "items" in names and "system.views" not in names and "a" * 121 not in names
    assert result["result"]["truncated"] is True
    assert result["result"]["schema_snapshot"]["metadata"]["skipped_unsupported_collections"] >= 2
    result = run("introspect_schema", limits={"max_bytes": 1024})
    assert not result["ok"] and result["definitely_no_external_effect"]
    for arguments, kind, value in [
        ({"filter": {}, "projection": {"x": 1}, "limit": {"$param": "v"}}, "integer", 2),
        ({"filter": {}, "projection": {"x": 1}, "sort": {"n": {"$param": "v"}}}, "integer", 1),
        ({"filter": {}, "projection": {"$param": "v"}}, "object", {"x": 1}),
    ]:
        template = deepcopy(request()["action"]["input"]["query_template"])
        template["query_definition_jsonb"]["arguments"] = arguments
        template["param_schema_jsonb"] = {"properties": {"v": {"type": kind}}}
        result = run("validate_query", query_template=template)
        assert result["ok"] and result["result"]["valid"], result
        assert result["result"]["validation_status"] == "sample_parameters_required"
        result = run("execute_template", query_template=template, params={"v": value})
        assert result["ok"] and result["result"]["rows"], result
        assert all("_id" not in row for row in result["result"]["rows"])
        result = run("execute_template", query_template=template)
        assert not result["ok"] and result["definitely_no_external_effect"]
        bad = 0 if kind == "integer" else {"private": 1}
        result = run("execute_template", query_template=template, params={"v": bad})
        assert not result["ok"] and result["definitely_no_external_effect"]
