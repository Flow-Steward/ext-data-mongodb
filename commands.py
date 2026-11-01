"""Compile closed CRUD operations to fixed MongoDB Toolbox tools."""

from __future__ import annotations
import json
from validation import Failure, collection_name, document, integer, require, validate_json

WRITES = {"insert_one", "insert_many", "update_one", "update_many", "delete_one", "delete_many"}
FIELDS = {
    "find": {"filter", "projection", "sort", "limit"},
    "find_one": {"filter", "projection"},
    "aggregate": {"pipeline", "limit"},
    "insert_one": {"document"},
    "insert_many": {"documents"},
    "update_one": {"filter", "update", "upsert"},
    "update_many": {"filter", "update", "upsert", "allow_many"},
    "delete_one": {"filter"},
    "delete_many": {"filter", "allow_many"},
}
STAGES = {
    "$match",
    "$project",
    "$group",
    "$sort",
    "$limit",
    "$skip",
    "$unwind",
    "$addFields",
    "$set",
    "$unset",
    "$count",
    "$replaceRoot",
    "$replaceWith",
}
UPDATES = {
    "$currentDate",
    "$inc",
    "$min",
    "$max",
    "$mul",
    "$rename",
    "$set",
    "$setOnInsert",
    "$unset",
    "$addToSet",
    "$pop",
    "$pull",
    "$push",
    "$pullAll",
    "$bit",
}


def literal(value):
    # Preserve literal Go-template delimiters without dynamic template parameters.
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).replace(
        "{{", r"\u007b\u007b"
    )


def compile_command(name, args, config, context, connection_id):
    if name not in FIELDS:
        raise Failure("unknown_action", "Unknown MongoDB action.")
    require(set(args) <= FIELDS[name] | {"connection_ref", "collection"}, "Unknown action input.")
    require(
        all(
            isinstance(context.get(key), str) and bool(context[key].strip())
            for key in ("account_id", "project_id")
        )
        and bool(connection_id),
        "Account and project scope required.",
        code="invalid_connection",
    )
    tool = {
        "type": "mongodb-" + name.replace("_", "-"),
        "database": config["database"],
        "collection": collection_name(args.get("collection")),
        "literalPayload": True,
    }
    arguments = {}
    if name in {"update_many", "delete_many"}:
        require(args.get("allow_many") is True, "Bulk changes require allow_many=true.")
    if name in {"find", "find_one", "update_one", "update_many", "delete_one", "delete_many"}:
        filt = document(args.get("filter", {}), "filter", nonempty=name in WRITES)
        tool["filterPayload"] = literal(filt)
    if name in {"find", "find_one"}:
        tool["projectPayload"] = literal(document(args.get("projection", {}), "projection"))
    if name == "find":
        sort = document(args.get("sort", {}), "sort")
        require(
            all(type(v) is int and v in (-1, 1) for v in sort.values()),
            "Sort directions must be 1 or -1.",
        )
        tool.update(
            sortPayload=literal(sort), limit=integer(args.get("limit", 20), "limit", 1, 100)
        )
    if name == "aggregate":
        pipeline = args.get("pipeline")
        require(
            isinstance(pipeline, list) and len(pipeline) <= 16,
            "Supply an array of at most 16 aggregation stages.",
        )
        validate_json(pipeline)
        require(
            all(
                isinstance(stage, dict) and len(stage) == 1 and next(iter(stage)) in STAGES
                for stage in pipeline
            ),
            "Only safe read aggregation stages are supported.",
        )
        tool.update(
            pipelinePayload=literal(
                [*pipeline, {"$limit": integer(args.get("limit", 20), "limit", 1, 100)}]
            ),
            canonical=True,
            readOnly=True,
            pipelineParams=[],
        )
    if name == "insert_one":
        arguments["data"] = literal(document(args.get("document"), "document"))
        tool["canonical"] = True
    if name == "insert_many":
        docs = args.get("documents")
        require(isinstance(docs, list) and 1 <= len(docs) <= 100, "Supply 1 to 100 documents.")
        for doc in docs:
            document(doc, "document")
        arguments["data"] = literal(docs)
        tool["canonical"] = True
    if name in {"update_one", "update_many"}:
        update = document(args.get("update"), "update", nonempty=True)
        require(
            all(key in UPDATES and isinstance(value, dict) for key, value in update.items()),
            "Use supported update operators; replacements and update pipelines are forbidden.",
        )
        upsert = args.get("upsert", False)
        require(type(upsert) is bool, "upsert must be boolean.")
        tool.update(updatePayload=literal(update), canonical=True, upsert=upsert, updateParams=[])
    return tool, arguments


def normalize_result(name, value, args):
    def valid(condition):
        require(condition, "Invalid MongoDB provider response.", code="invalid_provider_response")

    collection = args["collection"]
    valid(isinstance(value, list))
    if name not in WRITES:
        valid(
            len(value) <= (1 if name == "find_one" else args.get("limit", 20))
            and all(isinstance(doc, dict) for doc in value)
        )
        if name == "find_one":
            return {
                "collection": collection,
                "document": value[0] if value else None,
                "found": bool(value),
            }
        return {
            "collection": collection,
            "documents": value,
            "count": len(value),
            "found": bool(value),
        }
    valid(len(value) == 1 and isinstance(value[0], dict))
    result = value[0]
    expected = {
        "insert_one": {"inserted_id"},
        "insert_many": {"inserted_ids"},
        "update_one": {"matched_count", "modified_count", "upserted_count", "upserted_id"},
        "update_many": {"matched_count", "modified_count", "upserted_count", "upserted_id"},
        "delete_one": {"deleted_count"},
        "delete_many": {"deleted_count"},
    }[name]
    valid(set(result) == expected)
    for key in expected & {"matched_count", "modified_count", "upserted_count", "deleted_count"}:
        valid(type(result[key]) is int and 0 <= result[key] <= 9007199254740991)
    if name == "insert_many":
        valid(
            isinstance(result["inserted_ids"], list)
            and len(result["inserted_ids"]) == len(args["documents"])
        )
    applied = True
    if name.startswith("update_"):
        applied = result["modified_count"] > 0 or result["upserted_count"] > 0
    if name.startswith("delete_"):
        applied = result["deleted_count"] > 0
    return {"collection": collection, "applied": applied, **result}
