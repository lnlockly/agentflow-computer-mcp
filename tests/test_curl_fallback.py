"""System-curl fallback for the LLM POST.

Windows Defender's TLS MITM tears down Python's `urlopen` handshake
with `EOF occurred in violation of protocol (_ssl.c:2427)`, but the
Schannel-backed `curl.exe` (shipped in Windows 10 1803+ System32) often
survives the same MITM intact. After both urlopen retries fail we shell
out to curl with a non-stream payload as a last resort.

These tests inject a fake `subprocess.run` so we don't fire a real HTTP
request. They run on every host (no Windows-specific code paths).
"""
from __future__ import annotations

import json
import subprocess
import urllib.error

import pytest

from agentflow_computer_mcp.driver.loop import (
    LlmNetworkError,
    _curl_post_messages,
    post_llm_cancellable,
)


class _FakeProc:
    def __init__(self, rc: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_curl_post_messages_parses_success_body() -> None:
    body = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        assert cmd[0] == "curl"
        assert "x-api-key: KEY" in cmd
        assert "https://api.example.com/v1/messages" in cmd
        # Body comes in via stdin (--data-binary @-).
        assert kwargs["input"] == b'{"hello":"world"}'
        return _FakeProc(rc=0, stdout=body)

    result = _curl_post_messages(
        "https://api.example.com/v1/messages",
        "KEY",
        b'{"hello":"world"}',
        timeout=30,
        runner=fake_run,
    )
    assert result["content"][0]["text"] == "ok"


def test_curl_post_messages_nonzero_rc_raises() -> None:
    def fake_run(_cmd, **_kw):  # noqa: ANN001
        return _FakeProc(rc=6, stderr=b"Could not resolve host: api.example.com")

    with pytest.raises(LlmNetworkError, match="curl rc=6"):
        _curl_post_messages(
            "https://api.example.com/v1/messages",
            "KEY",
            b"{}",
            timeout=30,
            runner=fake_run,
        )


def test_curl_post_messages_missing_curl_raises() -> None:
    def fake_run(_cmd, **_kw):  # noqa: ANN001
        raise FileNotFoundError("curl")

    with pytest.raises(LlmNetworkError, match="curl missing"):
        _curl_post_messages(
            "https://api.example.com/v1/messages",
            "KEY",
            b"{}",
            timeout=30,
            runner=fake_run,
        )


def test_curl_post_messages_timeout_raises() -> None:
    def fake_run(_cmd, **_kw):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=["curl"], timeout=30)

    with pytest.raises(LlmNetworkError, match="curl timeout"):
        _curl_post_messages(
            "https://api.example.com/v1/messages",
            "KEY",
            b"{}",
            timeout=30,
            runner=fake_run,
        )


def test_curl_post_messages_empty_body_raises() -> None:
    def fake_run(_cmd, **_kw):  # noqa: ANN001
        return _FakeProc(rc=0, stdout=b"")

    with pytest.raises(LlmNetworkError, match="empty body"):
        _curl_post_messages(
            "https://api.example.com/v1/messages",
            "KEY",
            b"{}",
            timeout=30,
            runner=fake_run,
        )


def test_curl_post_messages_non_json_raises() -> None:
    def fake_run(_cmd, **_kw):  # noqa: ANN001
        return _FakeProc(rc=0, stdout=b"<html>500</html>")

    with pytest.raises(LlmNetworkError, match="non-JSON"):
        _curl_post_messages(
            "https://api.example.com/v1/messages",
            "KEY",
            b"{}",
            timeout=30,
            runner=fake_run,
        )


def test_post_llm_cancellable_uses_curl_when_urlopen_fails(monkeypatch) -> None:
    """When urlopen blows up twice (TLS EOF) the cancellable POST must
    fall back to curl and return the curl JSON response. The streaming
    SSE path is bypassed since curl returns the non-stream shape."""
    import agentflow_computer_mcp.driver.loop as loop_mod

    attempts = {"count": 0}

    def boom_urlopen(*_args, **_kwargs):
        attempts["count"] += 1
        raise urllib.error.URLError("EOF occurred in violation of protocol")

    monkeypatch.setattr(loop_mod.urllib.request, "urlopen", boom_urlopen)

    fallback_body = json.dumps(
        {
            "content": [{"type": "text", "text": "curl-served"}],
            "stop_reason": "end_turn",
        }
    ).encode()
    curl_calls: list[list[str]] = []

    def fake_curl(cmd, **kwargs):  # noqa: ANN001
        curl_calls.append(cmd)
        # Verify the streaming flag was stripped before curl was called.
        body = kwargs["input"].decode()
        assert '"stream"' not in body
        return _FakeProc(rc=0, stdout=fallback_body)

    monkeypatch.setattr(loop_mod.subprocess, "run", fake_curl)

    class _Flag:
        def is_set(self) -> bool:
            return False

    out = post_llm_cancellable(
        "https://api.example.com/v1/messages",
        "KEY",
        {"messages": [{"role": "user", "content": "hi"}]},
        abort_flag=_Flag(),
        timeout=10,
    )
    assert out["content"][0]["text"] == "curl-served"
    # urlopen tried twice (retry on EOF) before curl was reached.
    assert attempts["count"] == 2
    assert len(curl_calls) == 1


def test_post_llm_cancellable_raises_when_both_fail(monkeypatch) -> None:
    """If urlopen AND curl both fail, the daemon must surface
    `LlmNetworkError` so the worker translates it into a task_error."""
    import agentflow_computer_mcp.driver.loop as loop_mod

    def boom_urlopen(*_args, **_kwargs):
        raise urllib.error.URLError("EOF occurred in violation of protocol")

    def boom_curl(_cmd, **_kw):  # noqa: ANN001
        raise FileNotFoundError("curl")

    monkeypatch.setattr(loop_mod.urllib.request, "urlopen", boom_urlopen)
    monkeypatch.setattr(loop_mod.subprocess, "run", boom_curl)

    class _Flag:
        def is_set(self) -> bool:
            return False

    with pytest.raises(LlmNetworkError, match="urlopen.*curl"):
        post_llm_cancellable(
            "https://api.example.com/v1/messages",
            "KEY",
            {"messages": []},
            abort_flag=_Flag(),
            timeout=5,
        )
