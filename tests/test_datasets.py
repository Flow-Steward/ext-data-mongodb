"""Dataset host envelopes exercise real compiler and scope enforcement."""

import json

import pytest
from test_runtime import envelope, fake

import main


def request(name="preview_query", arguments=None):
    p = envelope(name)
    p["runtime_context"].update(
        account_id=None, project_id=None, step_id=None, step_execution_id=None
    )
    p["action"]["input"] = {
        "connection_config": {"host": "mongo.example", "database": "workflow"},
        "secret_refs": {},
        "dataset": {
            "dataset_id": "d",
            "resource_scope_jsonb": {
                "resources": [{"resource_id": "items", "columns": ["x", "n"]}]
            },
        },
        "query_template": {
            "query_template_id": "q",
            "dataset_id": "d",
            "query_language": "json",
            "query_definition_jsonb": {
                "resource_id": "items",
                "operation": "find",
                "arguments": arguments or {"filter": {}, "projection": {"x": 1}},
            },
        },
    }
    return p


def test_admin_preview_compiles_explicit_projection(monkeypatch):
    calls = fake(monkeypatch, [{"x": {"$numberInt": "2"}}])
    result = main.handle(request())
    assert result["ok"], result
    assert result["result"]["rows"] == [{"x": 2}]
    assert json.loads(calls[0][0][2]["projectPayload"]) == {"x": 1, "_id": 0}


@pytest.mark.parametrize(
    "value,kind",
    [(False, "boolean"), (0, "integer"), (None, "null"), ({"a": 1}, "object"), ([1, 2], "array")],
)
def test_typed_parameters(monkeypatch, value, kind):
    calls = fake(monkeypatch, [])
    p = request(arguments={"filter": {"x": {"$eq": {"$param": "v"}}}, "projection": {"x": 1}})
    p["action"]["input"]["query_template"]["param_schema_jsonb"] = {
        "properties": {"v": {"type": kind}}
    }
    p["action"]["input"]["params"] = {"v": value}
    assert main.handle(p)["ok"]
    assert json.loads(calls[0][0][2]["filterPayload"]) == {"x": {"$eq": value}}


@pytest.mark.parametrize(
    "arguments",
    [
        {"filter": {"secret": 1}, "projection": {"x": 1}},
        {"filter": {}, "projection": {"secret": 1}},
        {"filter": {"x": {"$where": "bad"}}, "projection": {"x": 1}},
        {"filter": {"x": {"$eq": {"$code": "bad"}}}, "projection": {"x": 1}},
        {"pipeline": [{"$lookup": {"from": "other"}}]},
        {"pipeline": [{"$match": {"secret": 1}}]},
        {"pipeline": [{"$project": {"x": "$secret"}}]},
    ],
)
def test_scope_and_unsafe_queries_fail_closed(monkeypatch, arguments):
    calls = fake(monkeypatch, [])
    p = request(arguments=arguments)
    if "pipeline" in arguments:
        p["action"]["input"]["query_template"]["query_definition_jsonb"]["operation"] = "aggregate"
    assert not main.handle(p)["ok"]
    assert not calls


def test_real_discovery_uses_metadata_and_samples(monkeypatch):
    calls = []

    def invoke(*a, **kw):
        calls.append(a[2])
        return [{"name": "items"}] if a[2].get("listCollections") else [{"x": 1}]

    monkeypatch.setattr(main.toolbox, "invoke", invoke)
    p = request("introspect_schema")
    p["action"]["input"].pop("dataset")
    p["action"]["input"].pop("query_template")
    result = main.handle(p)
    assert result["ok"], result
    assert result["result"]["resources"][0]["resource_id"] == "items"
    assert calls[0]["listCollections"] is True
    assert calls[1]["limit"] == 5


def test_result_scope_and_limits(monkeypatch):
    fake(monkeypatch, [{"secret": 1}])
    assert not main.handle(request())["ok"]
    fake(monkeypatch, [{"x": 1}] * 3)
    p = request()
    p["action"]["input"]["limits"] = {"max_preview_rows": 2, "max_runtime_rows": 3}
    assert not main.handle(p)["ok"]
    p["action"]["action_id"] = "execute_template"
    assert main.handle(p)["ok"]


def test_catalog_admin_null_scope(monkeypatch):
    p = request()
    p["mode"] = "query"
    p["query"] = {"query_id": "supported_connectors"}
    result = main.handle(p)
    assert result["ok"], result
    assert result["result"]["connectors"][0]["query_language"] == "json"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a["query_template"].update(dataset_id="other"),
        lambda a: a["query_template"]["query_definition_jsonb"].update(resource_id="Items"),
        lambda a: a["query_template"]["query_definition_jsonb"].update(operation="delete_one"),
        lambda a: a["query_template"]["query_definition_jsonb"].update(extra=True),
        lambda a: a.update(limits={"max_preview_rows": 101}),
        lambda a: a.update(limits={"timeout_seconds": 31}),
        lambda a: a.update(limits={"max_bytes": 409601}),
    ],
)
def test_ids_resource_case_and_budgets_reject_before_dispatch(monkeypatch, mutate):
    calls = fake(monkeypatch, [])
    p = request()
    mutate(p["action"]["input"])
    assert not main.handle(p)["ok"]
    assert not calls


@pytest.mark.parametrize(
    "value",
    [
        {"$where": "evil"},
        {"nested": [{"$code": "evil"}]},
        {"$lookup": {"from": "private"}},
        {"$scope": {}},
    ],
)
def test_unsafe_bound_objects_reject_before_dispatch(monkeypatch, value):
    calls = fake(monkeypatch, [])
    p = request(arguments={"filter": {"x": {"$eq": {"$param": "v"}}}, "projection": {"x": 1}})
    a = p["action"]["input"]
    a["query_template"]["param_schema_jsonb"] = {"properties": {"v": {"type": "object"}}}
    a["params"] = {"v": value}
    assert not main.handle(p)["ok"]
    assert not calls


def test_aggregate_is_read_only_explicit_and_bounded(monkeypatch):
    calls = fake(monkeypatch, [{"x": 1}])
    p = request(
        arguments={
            "pipeline": [
                {"$match": {"x": {"$gt": 0}}},
                {"$sort": {"x": 1}},
                {"$skip": 0},
                {"$limit": 8},
                {"$project": {"x": 1}},
            ]
        }
    )
    p["action"]["input"]["query_template"]["query_definition_jsonb"]["operation"] = "aggregate"
    p["action"]["input"]["limits"] = {"max_preview_rows": 2}
    result = main.handle(p)
    assert result["ok"], result
    command = calls[0][0][2]
    assert command["readOnly"] is True
    assert json.loads(command["pipelinePayload"])[-2:] == [
        {"$project": {"x": 1, "_id": 0}},
        {"$limit": 2},
    ]


def test_extended_json_preserves_nested_types_and_precision(monkeypatch):
    fake(
        monkeypatch,
        [
            {
                "x": {
                    "nested": [
                        {"$numberLong": "9007199254740993"},
                        {"$numberDouble": "1.5"},
                        False,
                        None,
                        {"$oid": "0123456789abcdef01234567"},
                        {"$date": {"$numberLong": "1700000000000"}},
                        {"$numberDecimal": "1.234567890123456789"},
                    ]
                }
            }
        ],
    )
    result = main.handle(request())
    assert result["ok"], result
    assert result["result"]["rows"][0]["x"]["nested"] == [
        "9007199254740993",
        1.5,
        False,
        None,
        "0123456789abcdef01234567",
        1700000000000,
        "1.234567890123456789",
    ]


def test_response_byte_budget(monkeypatch):
    fake(monkeypatch, [{"x": "x" * 2048}])
    p = request()
    p["action"]["input"]["limits"] = {"max_bytes": 1024}
    result = main.handle(p)
    assert not result["ok"] and result["retryable"] is False


def test_total_metadata_deadline(monkeypatch):
    import datasets

    clock = [0.0]
    monkeypatch.setattr(datasets.time, "monotonic", lambda: clock[0])

    def invoke(*args, **kwargs):
        clock[0] += 2
        return [{"name": "items"}]

    monkeypatch.setattr(main.toolbox, "invoke", invoke)
    p = request("introspect_schema")
    a = p["action"]["input"]
    a.pop("dataset")
    a.pop("query_template")
    a["limits"] = {"timeout_seconds": 1}
    assert main.handle(p)["error_code"] == "timeout"


def test_validation_does_not_dispatch_and_inference_returns_json_schema(monkeypatch):
    calls = fake(monkeypatch, [{"x": [False, 0, None]}])
    result = main.handle(request("validate_query"))
    assert result["ok"] and result["result"]["read_only"] and not calls
    result = main.handle(request("infer_output_schema"))
    assert result["ok"] and result["result"]["output_schema"]["properties"]["x"] == {
        "type": "array"
    }


@pytest.mark.parametrize("value", ["x" * 65536, [[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]])
def test_bound_parameter_limits_rechecked_after_binding(monkeypatch, value):
    calls = fake(monkeypatch, [])
    p = request(arguments={"filter": {"x": {"$eq": {"$param": "v"}}}, "projection": {"x": 1}})
    a = p["action"]["input"]
    a["query_template"]["param_schema_jsonb"] = {"properties": {"v": {"type": ["string", "array"]}}}
    a["params"] = {"v": value}
    assert not main.handle(p)["ok"] and not calls


def test_scoped_refresh_never_discovers_or_projects_private_fields(monkeypatch):
    calls = fake(monkeypatch, [{"x": 1}])
    result = main.handle(request("introspect_schema"))
    assert result["ok"], result
    assert len(calls) == 1
    command = calls[0][0][2]
    assert "listCollections" not in command and command["collection"] == "items"
    assert json.loads(command["projectPayload"]) == {"x": 1, "n": 1, "_id": 0}


def test_inference_preserves_heterogeneous_sample_types(monkeypatch):
    fake(monkeypatch, [{"x": False}, {"x": 0}, {"x": None}, {"x": {"a": 1}}, {"x": [1]}])
    result = main.handle(request("infer_output_schema"))
    assert result["ok"], result
    assert set(result["result"]["output_schema"]["properties"]["x"]["type"]) == {
        "boolean",
        "integer",
        "null",
        "object",
        "array",
    }


def test_empty_inference_reports_unknown_not_null(monkeypatch):
    fake(monkeypatch, [])
    result = main.handle(request("infer_output_schema"))
    assert result["ok"] and result["result"]["output_schema"]["properties"]["x"] == {}


def test_metadata_records_all_observed_types_and_nullability(monkeypatch):
    fake(monkeypatch, [{"x": False}, {"x": 0}, {"x": None}])
    result = main.handle(request("introspect_schema"))
    assert result["ok"], result
    column = result["result"]["resources"][0]["columns"][0]
    assert set(column["observed_types"]) == {"boolean", "integer", "null"}
    assert column["nullable"] is True
    assert column["value_type"] == "json"


@pytest.mark.parametrize("argument", ["filter", "sort", "projection"])
def test_bound_argument_objects_cannot_escape_field_scope(monkeypatch, argument):
    calls = fake(monkeypatch, [])
    arguments: dict = {"filter": {}, "projection": {"x": 1}, "sort": {}}
    arguments[argument] = {"$param": "injected"}
    p = request(arguments=arguments)
    a = p["action"]["input"]
    a["query_template"]["param_schema_jsonb"] = {"properties": {"injected": {"type": "object"}}}
    a["params"] = {"injected": {"private": 1}}
    result = main.handle(p)
    assert not result["ok"] and not calls


def test_review_metadata_assembled_response_respects_budget(monkeypatch):
    fake(monkeypatch, [{f"f{i}": 1 for i in range(15)}])
    p = request("introspect_schema")
    p["action"]["input"]["dataset"]["resource_scope_jsonb"]["resources"][0]["columns"] = None
    p["action"]["input"]["limits"] = {"max_bytes": 1024}
    result = main.handle(p)
    assert not result["ok"] and result["definitely_no_external_effect"]


def test_review_discovery_skips_unsupported_names_without_sampling(monkeypatch):
    calls = []

    def invoke(*args, **kwargs):
        command = args[2]
        calls.append(command)
        return (
            [{"name": "system.views"}, {"name": "a" * 121}, {"name": "items"}]
            if command.get("listCollections")
            else [{"x": 1}]
        )

    monkeypatch.setattr(main.toolbox, "invoke", invoke)
    p = request("introspect_schema")
    a = p["action"]["input"]
    a.pop("dataset")
    a.pop("query_template")
    result = main.handle(p)
    assert result["ok"], result
    assert [r["resource_id"] for r in result["result"]["resources"]] == ["items"]
    assert result["result"]["truncated"] is True
    assert [c["collection"] for c in calls[1:]] == ["items"]


@pytest.mark.parametrize(
    "arguments,kind",
    [
        ({"filter": {}, "projection": {"x": 1}, "limit": {"$param": "v"}}, "integer"),
        ({"filter": {}, "projection": {"x": 1}, "sort": {"x": {"$param": "v"}}}, "integer"),
        ({"filter": {}, "projection": {"$param": "v"}}, "object"),
    ],
)
def test_review_validate_unresolved_typed_positions(monkeypatch, arguments, kind):
    calls = fake(monkeypatch, [])
    p = request("validate_query", arguments)
    p["action"]["input"]["query_template"]["param_schema_jsonb"] = {
        "properties": {"v": {"type": kind}}
    }
    result = main.handle(p)
    assert result["ok"], result
    assert result["result"]["valid"] is True
    assert result["result"]["required_sample_params"] == ["v"]
    assert result["result"]["validation_status"] == "sample_parameters_required"
    assert result["result"]["validation_complete"] is False
    assert not calls


@pytest.mark.parametrize(
    "arguments,kind,value",
    [
        ({"filter": {}, "projection": {"x": 1}, "limit": {"$param": "v"}}, "integer", 0),
        ({"filter": {}, "projection": {"x": 1}, "sort": {"x": {"$param": "v"}}}, "integer", 0),
        ({"filter": {}, "projection": {"$param": "v"}}, "object", {"private": 1}),
    ],
)
def test_review_missing_and_bad_actual_bound_values_never_dispatch(
    monkeypatch, arguments, kind, value
):
    calls = fake(monkeypatch, [])
    p = request("execute_template", arguments)
    p["action"]["input"]["query_template"]["param_schema_jsonb"] = {
        "properties": {"v": {"type": kind}}
    }
    assert not main.handle(p)["ok"]
    p["action"]["input"]["params"] = {"v": value}
    assert not main.handle(p)["ok"] and not calls
