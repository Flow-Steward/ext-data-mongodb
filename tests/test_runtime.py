"""Bounded MongoDB actions, EJSON and external effect semantics."""

from copy import deepcopy
import json
import pytest
import main


def envelope(action, args=None):
    return {
        "contract_version": "extension_host_v2",
        "mode": "action",
        "runtime_context": {
            "group_id": None,
            "for_each_id": None,
            "step_id": "step",
            "step_execution_id": "exec",
            "account_id": "a",
            "project_id": "p",
            "external_effect_id": "claim",
        },
        "action": {
            "action_id": action,
            "input": {"connection_ref": "conn", "collection": "items", **(args or {})},
            "target": {
                "connection": {
                    "connection_id": "conn",
                    "connection_type": "mongodb",
                    "config": {"host": "mongo.example", "database": "workflow"},
                    "secrets": {},
                }
            },
        },
    }


def fake(monkeypatch, value):
    calls = []

    def invoke(*a, **kw):
        calls.append(deepcopy((a, kw)))
        return value

    monkeypatch.setattr(main.toolbox, "invoke", invoke)
    return calls


def test_find_preserves_ejson_and_bounded_fixed_payload(monkeypatch):
    doc = {"_id": {"$oid": "0123456789abcdef01234567"}, "n": {"$numberLong": "9007199254740993"}}
    calls = fake(monkeypatch, [doc])
    r = main.handle(envelope("find", {"filter": {"literal": "{{text}} $HOME"}}))
    assert r["ok"], r
    assert r["result"] == {"collection": "items", "documents": [doc], "count": 1, "found": True}
    command, arguments = calls[0][0][2:]
    assert command["type"] == "mongodb-find" and command["limit"] == 20
    assert arguments == {} and "{{" not in command["filterPayload"]
    assert json.loads(command["filterPayload"])["literal"] == "{{text}} $HOME"


def test_find_one_missing(monkeypatch):
    fake(monkeypatch, [])
    assert main.handle(envelope("find_one"))["result"] == {
        "collection": "items",
        "document": None,
        "found": False,
    }


@pytest.mark.parametrize(
    "action,args",
    [
        ("find", {"limit": 101}),
        ("find", {"limit": True}),
        ("find", {"skip": 1}),
        ("find", {"filter": {"x": {"$where": "evil"}}}),
        ("insert_one", {"document": {"n": 9007199254740992}}),
        ("aggregate", {"pipeline": [{"$lookup": {"from": "other"}}]}),
        ("aggregate", {"pipeline": [{"$match": {"x": {"$function": {}}}}]}),
        ("update_one", {"filter": {}, "update": {"$set": {"x": 1}}}),
        ("update_one", {"filter": {"x": 1}, "update": {"x": 2}}),
        ("update_many", {"filter": {"x": 1}, "update": {"$set": {"x": 2}}}),
        ("delete_many", {"filter": {"x": 1}, "allow_many": 1}),
        ("delete_one", {"filter": {}}),
        ("find", {"collection": "system.users"}),
        ("find", {"collection": "bad$collection"}),
        ("find", {"collection": "é" * 61}),
    ],
)
def test_invalid_inputs_never_dispatch(monkeypatch, action, args):
    calls = fake(monkeypatch, [])
    result = main.handle(envelope(action, args))
    assert not result["ok"] and result["error_code"] == "invalid_payload", result
    assert not calls


def test_aggregate_appends_limit_without_mutation(monkeypatch):
    calls = fake(monkeypatch, [])
    pipeline = [{"$match": {"x": 1}}]
    assert main.handle(envelope("aggregate", {"pipeline": pipeline, "limit": 7}))["ok"]
    assert json.loads(calls[0][0][2]["pipelinePayload"])[-1] == {"$limit": 7}
    assert len(pipeline) == 1


def test_writes_require_claim_and_validate_before_suppression(monkeypatch):
    calls = fake(monkeypatch, [{"inserted_id": {"$oid": "0123456789abcdef01234567"}}])
    p = envelope("insert_one", {"document": {"x": 1}})
    p["runtime_context"].pop("external_effect_id")
    assert main.handle(p)["error_code"] == "effect_claim_required"
    p["runtime_context"]["test_mode"] = True
    assert main.handle(p)["error_code"] == "test_mode_suppressed"
    p["action"]["input"]["document"] = []
    assert main.handle(p)["error_code"] == "invalid_payload"
    assert not calls


def test_dispatched_write_failure_is_uncertain_and_redacted(monkeypatch):
    from validation import Failure

    def invoke(*a, **kw):
        raise Failure("timeout", "Toolbox deadline exceeded.", dispatched=True)

    monkeypatch.setattr(main.toolbox, "invoke", invoke)
    r = main.handle(envelope("delete_one", {"filter": {"x": 1}}))
    assert r["external_effect_status"] == "timeout_unknown" and not r["retryable"]
    assert not r["definitely_no_external_effect"]


def test_connection_and_scope_validation(monkeypatch):
    calls = fake(monkeypatch, [])
    for mutate in [
        lambda p: p["action"]["target"]["connection"].update(connection_type="redis"),
        lambda p: p["runtime_context"].update(project_id=""),
        lambda p: p["action"]["target"]["connection"]["config"].update(uri="mongodb://evil"),
    ]:
        p = envelope("find")
        mutate(p)
        assert main.handle(p)["error_code"] == "invalid_connection"
    assert not calls


def test_insert_and_update_results(monkeypatch):
    oid = {"$oid": "0123456789abcdef01234567"}
    calls = fake(monkeypatch, [{"inserted_id": oid}])
    r = main.handle(envelope("insert_one", {"document": {"_id": oid, "x": "{{literal}}"}}))
    assert r["ok"] and r["result"]["inserted_id"] == oid and r["result"]["applied"]
    assert json.loads(calls[0][0][3]["data"])["x"] == "{{literal}}"
    fake(
        monkeypatch,
        [{"matched_count": 0, "modified_count": 0, "upserted_count": 0, "upserted_id": None}],
    )
    r = main.handle(
        envelope(
            "update_many", {"filter": {"x": 1}, "update": {"$set": {"x": 2}}, "allow_many": True}
        )
    )
    assert r["ok"] and not r["result"]["applied"]


def test_oversized_input_and_output_are_bounded(monkeypatch):
    calls = fake(monkeypatch, [])
    p = envelope("insert_one", {"document": {"text": "x" * (256 * 1024)}})
    r = main.handle(p)
    assert r["error_code"] == "invalid_payload" and not calls
    fake(monkeypatch, [{"text": "x" * (480 * 1024)}])
    r = main.handle(envelope("find"))
    assert r["error_code"] == "response_too_large" and r["definitely_no_external_effect"]


def test_invalid_write_provider_response_is_uncertain(monkeypatch):
    fake(monkeypatch, [{"deleted_count": "not a count"}])
    r = main.handle(envelope("delete_one", {"filter": {"x": 1}}))
    assert r["error_code"] == "invalid_provider_response"
    assert r["external_effect_status"] == "timeout_unknown" and not r["retryable"]


@pytest.mark.parametrize(
    "settings",
    [
        {"tls": "false"},
        {"srv": True, "port": 27018},
        {"request_timeout_seconds": 31},
        {"database": "bad.name"},
        {"host": "user@host"},
    ],
)
def test_invalid_connection_settings_are_redacted(monkeypatch, settings):
    calls = fake(monkeypatch, [])
    p = envelope("find")
    p["action"]["target"]["connection"]["config"].update(settings)
    r = main.handle(p)
    assert r["error_code"] == "invalid_connection" and not calls


def test_invalid_stdin_has_explicit_no_retry_effect_status(monkeypatch, capsys):
    from io import BytesIO
    from types import SimpleNamespace

    monkeypatch.setattr(main.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"not json")))
    assert main.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["retryable"] is False
    assert result["definitely_no_external_effect"] is True
    assert result["external_effect_status"] == "not_applied"


def test_fixed_tools_declare_empty_required_native_parameter_lists(monkeypatch):
    calls = fake(
        monkeypatch,
        [{"matched_count": 0, "modified_count": 0, "upserted_count": 0, "upserted_id": None}],
    )
    assert main.handle(envelope("update_one", {"filter": {"x": 1}, "update": {"$set": {"x": 2}}}))[
        "ok"
    ]
    assert calls[-1][0][2]["updateParams"] == []
    calls = fake(monkeypatch, [])
    assert main.handle(envelope("aggregate", {"pipeline": []}))["ok"]
    assert calls[-1][0][2]["pipelineParams"] == []
