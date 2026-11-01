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
