"""Read-only, explicit-field MongoDB Dataset queries; no workflow scope fabrication."""

from __future__ import annotations

import copy
import math
import re
import time

from commands import literal
from validation import (
    Failure,
    bounded_json,
    collection_name,
    connection_values,
    integer,
    require,
    validate_json,
)

ACTIONS = frozenset(
    {
        "test_connection",
        "introspect_schema",
        "validate_query",
        "preview_query",
        "execute_template",
        "infer_output_schema",
    }
)
FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TYPES = {"string", "integer", "number", "boolean", "null", "object", "array"}


def catalog():
    return [
        {
            "connector_id": "mongodb",
            "connector_type_id": "mongodb",
            "database_kind": "mongodb",
            "source_kind": "mongodb",
            "display_name": "MongoDB",
            "source_extension_id": "flowsteward.mongodb",
            "source_extension_name": "MongoDB",
            "source_provider": "MongoDB",
            "query_language": "json",
            "access_mode": "read_only",
        }
    ]


def bounded(value):
    bounded_json(value, 65536)
    nodes = 0

    def walk(v, depth):
        nonlocal nodes
        nodes += 1
        require(nodes <= 10000 and depth <= 20, "Dataset JSON exceeds depth or node budget.")
        if isinstance(v, dict):
            for k, c in v.items():
                require(isinstance(k, str), "JSON object keys must be strings.")
                walk(c, depth + 1)
        elif isinstance(v, list):
            for c in v:
                walk(c, depth + 1)

    walk(value, 0)
    validate_json(value)


def field(value):
    require(
        isinstance(value, str) and FIELD.fullmatch(value), "Use explicit top-level field names."
    )
    return value


def scope(dataset):
    require(isinstance(dataset, dict), "Dataset must be an object.")
    raw = dataset.get("resource_scope_jsonb", {}).get("resources")
    require(isinstance(raw, list) and 1 <= len(raw) <= 32, "Select 1–32 collections.")
    result = {}
    for item in raw:
        require(isinstance(item, dict), "Invalid resource scope.")
        name = collection_name(item.get("resource_id"))
        columns = item.get("columns")
        require(
            columns is None or isinstance(columns, list) and len(columns) <= 100,
            "Invalid field allowlist.",
        )
        require(name not in result, "Duplicate collection scope.")
        result[name] = None if columns is None else {field(c) for c in columns}
    return result


def kind(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def bind(arguments, template, params, execution):
    schema = template.get("param_schema_jsonb", {})
    require(
        isinstance(schema, dict) and isinstance(schema.get("properties", {}), dict),
        "Invalid parameter schema.",
    )
    declarations = schema.get("properties", {})
    require(isinstance(params, dict), "Parameters must be an object.")
    names = set()
    missing = {}

    def visit(v):
        if isinstance(v, dict):
            if "$param" in v:
                require(
                    set(v) == {"$param"} and isinstance(v["$param"], str),
                    "Use single-member parameter markers.",
                )
                name = v["$param"]
                names.add(name)
                require(
                    name in declarations and isinstance(declarations[name], dict),
                    "Undeclared parameter.",
                )
                types = declarations[name].get("type")
                types = types if isinstance(types, list) else [types]
                require(bool(types) and all(t in TYPES for t in types), "Invalid parameter types.")
                if name not in params:
                    require(not execution, "Missing query parameter.")
                    missing[name] = set(types)
                    return dict(v)
                value = params[name]
                require(
                    kind(value) in types or kind(value) == "integer" and "number" in types,
                    "Parameter type mismatch.",
                )
                bounded(value)
                return copy.deepcopy(value)
            return {k: visit(c) for k, c in v.items()}
        if isinstance(v, list):
            return [visit(c) for c in v]
        return v

    value = visit(arguments)
    require(set(params) <= names, "Unknown query parameter.")
    bounded(value)
    return value, missing


def compile_query(args, execution, limit):
    dataset = args.get("dataset")
    allowed = scope(dataset)
    t = args.get("query_template")
    require(
        isinstance(t, dict) and t.get("query_language") == "json",
        "A JSON query template is required.",
    )
    require(
        isinstance(t.get("query_template_id"), str) and bool(t["query_template_id"]),
        "Saved query identifier required.",
    )
    require(
        isinstance(dataset.get("dataset_id"), str)
        and bool(dataset["dataset_id"])
        and t.get("dataset_id") == dataset["dataset_id"],
        "Saved query belongs to another dataset.",
    )
    definition = t.get("query_definition_jsonb")
    bounded(definition)
    require(
        isinstance(definition, dict)
        and set(definition) == {"resource_id", "operation", "arguments"},
        "Invalid JSON query definition.",
    )
    resource = definition["resource_id"]
    require(
        isinstance(resource, str) and resource in allowed, "Collection is outside dataset scope."
    )
    operation = definition["operation"]
    require(operation in {"find", "aggregate"}, "Unsupported Dataset operation.")
    raw = definition["arguments"]
    require(isinstance(raw, dict), "Arguments must be an object.")
    a, missing = bind(raw, t, args.get("params", {}), execution)
    columns = allowed[resource]

    def unresolved(value, expected):
        if isinstance(value, dict) and set(value) == {"$param"} and value.get("$param") in missing:
            require(
                bool(missing[value["$param"]] & expected),
                "Parameter type cannot satisfy this query position.",
            )
            return True
        return False

    def number(value, name, low, high):
        return low if unresolved(value, {"integer", "number"}) else integer(value, name, low, high)

    def check_field(k):
        field(k)
        require(columns is None or k in columns, "Field is outside dataset scope.")

    def filt(v):
        if unresolved(v, {"object"}):
            return
        require(isinstance(v, dict), "Filter must be an object.")
        for k, c in v.items():
            if k in {"$and", "$or", "$nor"}:
                if unresolved(c, {"array"}):
                    continue
                require(isinstance(c, list) and 1 <= len(c) <= 32, "Invalid logical filter.")
                for child in c:
                    filt(child)
            else:
                check_field(k)
                if unresolved(c, TYPES):
                    continue
                if isinstance(c, dict) and any(key.startswith("$") for key in c):
                    require(
                        set(c)
                        <= {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists"},
                        "Unsupported filter operator.",
                    )
                    for op, val in c.items():
                        if op in {"$in", "$nin"}:
                            if unresolved(val, {"array"}):
                                continue
                            require(isinstance(val, list), "Membership filter requires an array.")
                        if op == "$exists":
                            if unresolved(val, {"boolean"}):
                                continue
                            require(type(val) is bool, "Existence filter requires boolean.")

    def sort(v):
        if unresolved(v, {"object"}):
            return
        require(isinstance(v, dict), "Sort must be an object.")
        for k, c in v.items():
            check_field(k)
            require(
                unresolved(c, {"integer", "number"}) or type(c) is int and c in {-1, 1},
                "Invalid sort direction.",
            )

    def projection(v):
        if unresolved(v, {"object"}):
            return []
        require(isinstance(v, dict) and bool(v) and len(v) <= 100, "Select explicit output fields.")
        selected = []
        for k, c in v.items():
            if k == "_id" and type(c) is int and c == 0:
                continue
            check_field(k)
            require(
                unresolved(c, {"integer", "number"}) or type(c) is int and c == 1,
                "Use inclusion projections only.",
            )
            selected.append(k)
        require(bool(selected), "Select output fields.")
        return selected

    tool = {
        "type": "mongodb-" + operation,
        "database": None,
        "collection": resource,
        "literalPayload": True,
    }
    if operation == "find":
        require(set(a) <= {"filter", "projection", "sort", "limit"}, "Unknown find argument.")
        filt(a.get("filter", {}))
        sort(a.get("sort", {}))
        selected = projection(a.get("projection"))
        n = number(a.get("limit", limit), "limit", 1, 100)
        tool.update(
            filterPayload=literal(a.get("filter", {})),
            sortPayload=literal(a.get("sort", {})),
            projectPayload=literal(
                {**a["projection"], **({} if "_id" in selected else {"_id": 0})}
            ),
            limit=min(n, limit),
        )
    else:
        require(set(a) == {"pipeline"}, "Aggregate requires a pipeline.")
        pipeline = a["pipeline"]
        unknown_pipeline = unresolved(pipeline, {"array"})
        if unknown_pipeline:
            pipeline = []
        require(isinstance(pipeline, list) and len(pipeline) <= 16, "Use at most 16 stages.")
        selected = [] if unknown_pipeline else None
        for stage in pipeline:
            require(isinstance(stage, dict) and len(stage) == 1, "Invalid aggregation stage.")
            op, v = next(iter(stage.items()))
            require(
                op in {"$match", "$sort", "$skip", "$limit", "$project"},
                "Unsupported Dataset aggregation stage.",
            )
            if op == "$match":
                filt(v)
            elif op == "$sort":
                sort(v)
            elif op == "$project":
                fields = projection(v)
                require(
                    selected is None or not selected or set(fields) <= set(selected),
                    "Projection cannot restore removed fields.",
                )
                selected = fields
            else:
                number(v, op, 1 if op == "$limit" else 0, 100 if op == "$limit" else 10000)
        require(selected is not None, "Aggregate requires explicit output projection.")
        pipeline = [
            *pipeline,
            {"$project": {**{k: 1 for k in selected}, **({} if "_id" in selected else {"_id": 0})}},
            {"$limit": limit},
        ]
        tool.update(
            pipelinePayload=literal(pipeline), pipelineParams=[], canonical=True, readOnly=True
        )
    return tool, selected, resource, sorted(missing)


def decode(v):
    if isinstance(v, list):
        return [decode(c) for c in v]
    if not isinstance(v, dict):
        return v
    if len(v) == 1:
        k, c = next(iter(v.items()))
        if k in {"$numberInt", "$numberLong"}:
            n = int(c)
            return n if abs(n) <= 9007199254740991 else str(n)
        if k in {"$numberDouble", "$numberDecimal"}:
            # Decimal stays a string to avoid losing arbitrary decimal precision.
            if k == "$numberDecimal":
                return str(c)
            number = float(c)
            require(
                math.isfinite(number),
                "Nonfinite provider number.",
                code="invalid_provider_response",
            )
            return number
        if k == "$oid":
            return str(c)
        if k == "$date":
            return decode(c)
    return {k: decode(c) for k, c in v.items()}


def handle(name, args, invoke):
    result: dict
    require(
        set(args)
        <= {"connection_config", "secret_refs", "dataset", "query_template", "params", "limits"},
        "Unknown Dataset input.",
    )
    refs = args.get("secret_refs", {})
    require(isinstance(refs, dict), "Invalid credentials.")
    config, secrets = connection_values(
        {
            "config": args.get("connection_config"),
            "secrets": refs.get("values", refs.get("refs", refs)),
        }
    )
    limits = args.get("limits", {})
    require(
        isinstance(limits, dict)
        and set(limits) <= {"max_preview_rows", "max_runtime_rows", "timeout_seconds", "max_bytes"},
        "Invalid limits.",
    )
    preview = integer(limits.get("max_preview_rows", 20), "max_preview_rows", 1, 100)
    runtime = integer(limits.get("max_runtime_rows", 100), "max_runtime_rows", 1, 100)
    budget = integer(limits.get("max_bytes", 400 * 1024), "max_bytes", 1024, 400 * 1024)
    timeout = integer(
        limits.get("timeout_seconds", config["request_timeout_seconds"]), "timeout_seconds", 1, 30
    )
    deadline = time.monotonic() + min(timeout, config["request_timeout_seconds"])

    def call(tool, n):
        left = deadline - time.monotonic()
        require(left > 0, "Dataset deadline exceeded.", code="timeout")
        value = invoke(
            config, secrets, {**tool, "database": config["database"]}, {}, timeout_seconds=left
        )
        require(time.monotonic() < deadline, "Dataset deadline exceeded.", code="timeout")
        require(
            isinstance(value, list) and len(value) <= n and all(isinstance(r, dict) for r in value),
            "Invalid bounded provider rows.",
            code="invalid_provider_response",
        )
        bounded_json(value, budget)
        return decode(value)

    metadata = {
        "type": "mongodb-find",
        "listCollections": True,
        "literalPayload": True,
        "collection": "__metadata__",
        "filterPayload": "{}",
        "limit": 33,
    }
    if name == "test_connection":
        call(metadata, 33)
        return {"status": "ok", "connector_id": "mongodb"}
    if name == "introspect_schema":
        allowed = scope(args["dataset"]) if "dataset" in args else None
        discovered = call(metadata, 33) if allowed is None else [{"name": n} for n in allowed]
        truncated = len(discovered) > 32
        skipped = 0
        resources = []
        remaining = 100
        for item in discovered[:32]:
            raw_name = item.get("name")
            require(
                isinstance(raw_name, str) and bool(raw_name),
                "Invalid collection metadata.",
                code="invalid_provider_response",
            )
            try:
                name = collection_name(raw_name)
            except Failure:
                skipped += 1
                truncated = True
                continue
            fields = None if allowed is None else allowed[name]
            projection = (
                {}
                if fields is None
                else {**{k: 1 for k in fields}, **({} if "_id" in fields else {"_id": 0})}
            )
            # Empty selected field scopes need no document inspection.
            rows = (
                []
                if fields == set()
                else call(
                    {
                        "type": "mongodb-find",
                        "collection": name,
                        "literalPayload": True,
                        "filterPayload": "{}",
                        "projectPayload": literal(projection),
                        "limit": 5,
                    },
                    5,
                )
            )
            columns: dict[str, dict] = {}
            for row in rows:
                require(
                    fields is None or set(row) <= fields,
                    "Metadata escaped field scope.",
                    code="invalid_provider_response",
                )
                for k, v in row.items():
                    if not FIELD.fullmatch(k):
                        continue
                    if k not in columns and remaining == 0:
                        truncated = True
                        continue
                    if k not in columns:
                        columns[k] = {
                            "name": k,
                            "type": kind(v),
                            "data_type": kind(v),
                            "value_type": kind(v),
                            "nullable": v is None,
                        }
                        columns[k]["observed_types"] = []
                        remaining -= 1
                    observed = columns[k]["observed_types"]
                    if kind(v) not in observed:
                        observed.append(kind(v))
                    columns[k]["nullable"] = columns[k]["nullable"] or v is None
                    columns[k]["output_schema"] = {
                        "type": observed[0] if len(observed) == 1 else list(observed)
                    }
                    if len(observed) > 1:
                        columns[k]["value_type"] = "json"
                        columns[k]["type"] = "mixed"
                        columns[k]["data_type"] = "mixed"
            resources.append(
                {
                    "resource_id": name,
                    "name": name,
                    "resource_type": "collection",
                    "selectable": True,
                    "columns": list(columns.values()),
                    "indexes": [],
                    "metadata": {"sampled": True, "sample_rows": len(rows), "sample_limit": 5},
                }
            )
        snapshot = {
            "schema_version": "dataset_schema_snapshot.v1",
            "source_kind": "mongodb",
            "resources": resources,
            "truncated": truncated,
            "metadata": {
                "sampled": True,
                "collection_limit": 32,
                "column_limit": 100,
                "discovery_scan_limit": 33,
                "skipped_unsupported_collections": skipped,
                "connector_id": "mongodb",
            },
        }
        result = {
            "schema_snapshot": snapshot,
            "resources": resources,
            "resource_count": len(resources),
            "truncated": truncated,
        }
        bounded_json(result, budget)
        return result
    limit = runtime if name == "execute_template" else preview
    tool, selected, resource, missing = compile_query(args, name != "validate_query", limit)
    if name == "validate_query":
        result = {
            "valid": True,
            "read_only": True,
            "validation_complete": not missing,
            "referenced_resources": [resource],
        }
        if missing:
            result.update(
                validation_status="sample_parameters_required", required_sample_params=missing
            )
        return result
    rows = call(tool, min(limit, tool.get("limit", limit)))
    require(
        all(set(row) <= set(selected) for row in rows),
        "Unexpected output fields.",
        code="invalid_provider_response",
    )
    properties: dict[str, dict] = {}
    for column in selected:
        observed = sorted({kind(row[column]) for row in rows if column in row})
        properties[column] = (
            {"type": observed[0] if len(observed) == 1 else observed} if observed else {}
        )
    schema = {"type": "object", "properties": properties}
    result = (
        {"output_schema": schema}
        if name == "infer_output_schema"
        else {
            "rows": rows,
            "row_count": len(rows),
            "output_schema": schema,
            "truncated": len(rows) == limit,
        }
    )
    bounded_json(result, budget)
    return result
