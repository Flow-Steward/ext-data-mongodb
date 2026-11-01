"""Closed, bounded MongoDB connection and Extended JSON validation."""

from __future__ import annotations
import ipaddress
import json
import math


class Failure(Exception):
    def __init__(self, code, message, *, dispatched=False):
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched


def require(condition, message, *, code="invalid_payload"):
    if not condition:
        raise Failure(code, message)


def text(value, name, maximum=65536, *, nonempty=False):
    require(isinstance(value, str), f"{name} must be text.")
    try:
        require(len(value.encode("utf-8")) <= maximum, f"{name} exceeds its UTF-8 byte limit.")
    except UnicodeError as exc:
        raise Failure("invalid_payload", f"{name} must be valid UTF-8.") from exc
    require(not nonempty or bool(value.strip()), f"{name} must be nonempty.")
    require("\x00" not in value, f"{name} contains a null character.")
    return value


def integer(value, name, low, high):
    require(
        type(value) is int and low <= value <= high,
        f"{name} must be an integer from {low} to {high}.",
    )
    return value


def bounded_json(value, limit=256 * 1024):
    try:
        require(
            len(json.dumps(value, allow_nan=False).encode()) <= limit,
            "Input exceeds the JSON byte limit.",
        )
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise Failure("invalid_payload", "Bounded valid JSON is required.") from exc


FORBIDDEN = {
    "$where",
    "$function",
    "$accumulator",
    "$out",
    "$merge",
    "$lookup",
    "$unionWith",
    "$graphLookup",
}


def document(value, name, *, nonempty=False):
    require(
        isinstance(value, dict) and (not nonempty or bool(value)),
        f"{name} must be an object" + (" with a filter." if nonempty else "."),
    )
    validate_json(value)
    return value


def validate_json(value, depth=0):
    require(depth <= 64, "JSON nesting exceeds the supported limit.")
    if isinstance(value, dict):
        for key, child in value.items():
            text(key, "document key", 1024)
            require(
                key not in FORBIDDEN and key not in {"$code", "$scope"},
                "Unsafe MongoDB operator or JavaScript BSON value.",
            )
            validate_json(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_json(child, depth + 1)
    elif type(value) is int:
        require(
            abs(value) <= 9007199254740991,
            "Use Extended JSON for integers outside the safe JSON range.",
        )
    elif type(value) is float:
        require(math.isfinite(value), "Use finite JSON numbers or Extended JSON.")
    elif isinstance(value, str):
        # BSON string values may contain null; keys/identifiers may not.
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise Failure("invalid_payload", "Document strings must be UTF-8.") from exc
    else:
        require(value is None or type(value) is bool, "A JSON value is required.")


def collection_name(value):
    value = text(value, "collection", 120, nonempty=True)
    require(
        "$" not in value
        and not value.startswith("system.")
        and not value.startswith(".")
        and not value.endswith("."),
        "Invalid collection name.",
    )
    return value


def connection_values(connection):
    try:
        config = connection.get("config") or {}
        secrets = connection.get("secrets") or {}
        require(
            isinstance(config, dict) and isinstance(secrets, dict),
            "Connection must contain objects.",
        )
        require(
            set(config)
            <= {"host", "port", "database", "auth_source", "srv", "tls", "request_timeout_seconds"},
            "Unknown connection setting.",
        )
        require(set(secrets) <= {"username", "password"}, "Unknown credential setting.")
        host = text(config.get("host"), "host", 253, nonempty=True)
        require(
            not any(c.isspace() or c in "/\\@?#[]$%" for c in host),
            "Use a hostname or IP without URL components.",
        )
        if ":" in host:
            ipaddress.IPv6Address(host)
        database = text(config.get("database"), "database", 63, nonempty=True)
        auth = text(config.get("auth_source", "admin"), "auth_source", 63, nonempty=True)
        for name in (database, auth):
            require(not any(c in '/\\. "$' for c in name), "Invalid database name.")
        srv, tls = config.get("srv", False), config.get("tls", True)
        require(type(srv) is bool and type(tls) is bool, "srv and tls must be boolean.")
        port = integer(config.get("port", 27017), "port", 1, 65535)
        require(
            not srv or (":" not in host and port == 27017 and tls),
            "SRV requires a DNS hostname, default port and TLS.",
        )
        for key in ("username", "password"):
            text(secrets.get(key, ""), key, 8192)
        require(
            not secrets.get("password") or bool(secrets.get("username")),
            "A username is required with a password.",
        )
        return {
            "host": host,
            "port": port,
            "database": database,
            "auth_source": auth,
            "srv": srv,
            "tls": tls,
            "request_timeout_seconds": integer(
                config.get("request_timeout_seconds", 15), "request_timeout_seconds", 1, 30
            ),
        }, secrets
    except (Failure, ValueError, TypeError) as exc:
        raise Failure(
            "invalid_connection", "Invalid MongoDB connection configuration or credentials."
        ) from exc
