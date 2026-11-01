"""Bounded native transport, redaction, endpoint and literal preservation."""

import json
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
import pytest
import toolbox
from validation import Failure, connection_values


def config(**kw):
    return connection_values({"config": {"host": "mongo.example", "database": "app", **kw}})[0]


def test_uri_encodes_credentials_and_only_controlled_options(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "true")
    c = config()
    raw = toolbox.managed_config(
        c,
        {"username": "a@b", "password": "$HOME:p/?"},
        {"type": "mongodb-find", "filterPayload": '{"$eq":"{{text}}"}'},
        {},
    )
    source, tool = [json.loads(part) for part in raw.split("\n---\n")]
    assert "$" not in raw
    assert source["allowPrivate"] is True
    assert "a%40b:%24HOME%3Ap%2F%3F@" in source["uri"]
    options = parse_qs(urlsplit(source["uri"]).query)
    assert options["retryWrites"] == ["false"] and options["retryReads"] == ["false"]
    assert options["w"] == ["majority"] and options["tls"] == ["true"]
    assert tool["filterPayload"] == '{"$eq":"{{text}}"}'


def test_mcp_result_preserves_multi_block_array_and_empty():
    response = {
        "result": {
            "content": [
                {"type": "text", "text": '{"x":{"$numberInt":"1"}}'},
                {"type": "text", "text": '{"x":2}'},
            ]
        }
    }
    assert toolbox._result(response) == [{"x": {"$numberInt": "1"}}, {"x": 2}]
    assert toolbox._result({"result": {"content": []}}) == []


def test_provider_diagnostics_are_redacted():
    with pytest.raises(Failure) as exc:
        toolbox._result(
            {
                "result": {
                    "isError": True,
                    "content": [
                        {"text": "Authentication failed password=secret uri=mongodb://secret"}
                    ],
                }
            }
        )
    assert exc.value.code == "authentication_failed" and "secret" not in str(exc.value)


def test_endpoint_rejects_mixed_private_without_host_optin(monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(toolbox, "resolve_pinned_ips", lambda *a, **kw: ["8.8.8.8", "127.0.0.1"])
    with pytest.raises(Failure, match="Private endpoints"):
        toolbox._endpoint(config())
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    with pytest.raises(Failure, match="Public endpoints require TLS"):
        toolbox._endpoint(config(tls=False))


def test_endpoint_private_allowed_and_srv_deferred_to_native(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(toolbox, "resolve_pinned_ips", lambda *a, **kw: ["10.0.0.1"])
    assert toolbox._endpoint(config(tls=False)) == ("10.0.0.1",)
    assert toolbox._endpoint(config(srv=True)) is None


def child(code):
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def test_stalled_stdin_obeys_deadline():
    process = child("import time; time.sleep(10)")
    started = time.monotonic()
    try:
        with pytest.raises(Failure, match="deadline"):
            toolbox._send(process, {"x": "z" * 200000}, started + 0.1)
    finally:
        toolbox._stop(process)
    assert time.monotonic() - started < 3 and process.poll() is not None


def test_flooding_stdout_obeys_shared_output_cap():
    process = child("import os; os.write(1,b'x'*1100000)")
    try:
        with pytest.raises(Failure) as exc:
            toolbox._read(process, 1, time.monotonic() + 3, {"bytes": 0, "buffer": bytearray()})
        assert exc.value.code == "response_too_large"
    finally:
        toolbox._stop(process)


def test_startup_failure_not_marked_dispatched(monkeypatch):
    monkeypatch.setattr(toolbox, "_binary", lambda: "/missing/toolbox")
    monkeypatch.setattr(toolbox, "_endpoint", lambda config: ("127.0.0.1",))
    with pytest.raises(Failure) as exc:
        toolbox.invoke(config(), {}, {"type": "mongodb-find"}, {}, timeout_seconds=1)
    assert not exc.value.dispatched and "missing" not in str(exc.value)


def test_native_result_budget_classification():
    with pytest.raises(Failure) as exc:
        toolbox._result(
            {
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "MongoDB response exceeds result byte limit",
                        }
                    ],
                }
            }
        )
    assert exc.value.code == "response_too_large"
