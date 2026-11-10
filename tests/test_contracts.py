"""MongoDB public contract parity."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
READS = {"find", "find_one", "aggregate"}
WRITES = {
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "delete_one",
    "delete_many",
}


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


def test_identity_and_parity():
    m = load("extension.yaml")
    d = load("extension-descriptor.yaml")
    assert m["extension_id"] == d["extension_id"] == "flowsteward.mongodb"
    assert m["version"] == d["release_version"] == "1.1.3"
    assert m["features"] == ["tool", "action", "dataset_connector"]
    assert m["runtime"] == d["runtime"]
    assert m["egress"] == {"connection_fields": ["mongodb.host"]}
    ops = load("contracts/operation_manifest.yaml")["operations"]
    acts = load("ui/actions/actions.yaml")["actions"]
    forms = load("contracts/step_ui_manifest.yaml")["forms"]
    assert {o["operation_id"] for o in ops} == READS | WRITES
    assert {a["action_id"] for a in acts if a.get("workflow_visible")} == READS | WRITES
    assert {a["action_id"] for a in acts if not a.get("workflow_visible")} == {
        "test_connection",
        "introspect_schema",
        "validate_query",
        "preview_query",
        "execute_template",
        "infer_output_schema",
    }
    connector = m["runtime"]["extension_contract_v2"]["dataset_connectors"][0]
    assert connector["query_language"] == "json"
    assert connector["capabilities"]["write_queries"] is False
    assert {f["operation_id"] for f in forms} == READS | WRITES
    assert {e["operation_id"] for e in m["external_effects"]} == WRITES
    for op in ops:
        assert op["connection_type_ids"] == ["mongodb"]
        act = next(a for a in acts if a["action_id"] == op["operation_id"])
        form = next(f for f in forms if f["operation_id"] == op["operation_id"])
        assert {i["name"] for i in op["inputs"]} == {a["name"] for a in act["parameters"]}
        assert {f["name"] for f in form["fields"]} == {i["name"] for i in op["inputs"]} - {
            "connection_ref"
        }


def test_connection_contract():
    c = load("contracts/connection_types.yaml")["connection_types"]
    assert len(c) == 1 and c[0]["connection_type_id"] == "mongodb"
    assert c[0]["config_schema"]["additionalProperties"] is False
    assert set(c[0]["config_schema"]["properties"]) == {
        "host",
        "port",
        "srv",
        "tls",
        "database",
        "auth_source",
        "request_timeout_seconds",
    }
    assert c[0]["secret_schema"]["additionalProperties"] is False
    assert set(c[0]["secret_schema"]["properties"]) == {"username", "password"}
    assert c[0]["capabilities"]["workflow_trigger"] is False


def test_native_dataset_requires_platform_1_5_in_both_descriptors():
    minimums = {
        filename: load(filename)["runtime"]["compatibility"]["platform_min"]
        for filename in ("extension.yaml", "extension-descriptor.yaml")
    }
    assert minimums == {
        "extension.yaml": "1.5.0",
        "extension-descriptor.yaml": "1.5.0",
    }
