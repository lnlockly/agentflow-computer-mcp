"""Unit tests for the swappable code-agent backend registry.

The hosted-daemon flow lives in :mod:`agent_brief`; the backends here
own the per-tool argv + env shape so a new CLI can land without
touching agent_brief. Tests cover the contract:

* ``aider`` backend produces the production argv + env layering
* ``opencode`` backend reproduces the pre-aider behaviour for fallback
* registry honours ``CODE_AGENT_BACKEND`` env var
* unknown slugs raise with a list of valid ones
* generic CLI backend substitutes ``{brief}`` / ``{project_dir}`` tokens
* legacy ``opencode_bin=`` kwarg in agent_brief still routes correctly
"""

from __future__ import annotations

import pytest

from agentflow_computer_mcp.driver.tools import agent_brief as ab
from agentflow_computer_mcp.driver.tools import code_agents
from agentflow_computer_mcp.driver.tools.code_agents.aider import AiderBackend
from agentflow_computer_mcp.driver.tools.code_agents.cli_generic import GenericCLIBackend
from agentflow_computer_mcp.driver.tools.code_agents.opencode import OpenCodeBackend


# ---------------------------------------------------------------------------
# Backend command shape — pin the exact CLI flags + env keys each backend
# emits. A regression here means the spawn ran a different tool than the
# operator asked for, which is the whole point of the abstraction.
# ---------------------------------------------------------------------------


def test_aider_backend_command_includes_cli_flags():
    backend = AiderBackend()
    argv, env = backend.build_command(
        brief="build a coffee landing",
        project_dir="/workspace/proj-demo",
        api_key="af_live_xxx",
        api_base="https://agentflow.website/_agents",
    )
    assert argv[0] == "aider"
    # The flag set is what makes aider safe to run unattended; the
    # production spawn relied on every one of these before the refactor.
    for flag in (
        "--yes-always",
        "--no-pretty",
        "--no-auto-commits",
        "--no-stream",
        "--no-git",
        "--no-show-model-warnings",
        "--edit-format",
        "--map-tokens",
        "--message",
    ):
        assert flag in argv, f"{flag} missing from aider argv"
    # brief is the last argv slot so a brief containing flag-shaped text
    # cannot be misparsed by argparse.
    assert argv[-1] == "build a coffee landing"
    assert argv[-2] == "--message"
    # OpenAI shim env — aider reads OPENAI_API_BASE + OPENAI_API_KEY
    # because we tell it the model is openai/flow.
    assert env["OPENAI_API_BASE"] == "https://agentflow.website/_agents/llm/v1"
    assert env["OPENAI_API_KEY"] == "af_live_xxx"
    # Analytics + update checks would noisily fail under egress
    # restrictions and clutter the agent_log stream.
    assert env["AIDER_ANALYTICS"] == "false"
    assert env["AIDER_CHECK_UPDATE"] == "false"


def test_aider_backend_omits_openai_env_when_api_key_blank():
    # No AF_API_KEY → no OpenAI shim env. The spawn will fail at the
    # auth layer but the argv stays clean so logs are unambiguous.
    backend = AiderBackend()
    _, env = backend.build_command(
        brief="x", project_dir="/tmp", api_key="", api_base="https://x/_agents"
    )
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_API_BASE" not in env


def test_aider_backend_honours_custom_binary_path():
    # Image variants ship aider at non-standard paths; the constructor
    # passes the path straight through so the spawn picks up the right
    # executable.
    backend = AiderBackend(binary="/opt/agentflow/bin/aider")
    argv, _ = backend.build_command(
        brief="x", project_dir="/tmp", api_key="k", api_base="https://x/_agents"
    )
    assert argv[0] == "/opt/agentflow/bin/aider"


def test_opencode_backend_command_unchanged():
    # Pre-aider shape: ``opencode run <brief>``. Kept around so a flip
    # via CODE_AGENT_BACKEND=opencode reproduces the legacy behaviour
    # without code changes.
    backend = OpenCodeBackend()
    argv, env = backend.build_command(
        brief="static landing",
        project_dir="/workspace/proj-demo",
        api_key="af_live_xxx",
        api_base="https://agentflow.website/_agents",
    )
    assert argv == ["opencode", "run", "static landing"]
    assert env["OPENAI_API_BASE"] == "https://agentflow.website/_agents/llm/v1"
    assert env["OPENAI_API_KEY"] == "af_live_xxx"
    # Model alias flows through OPENCODE_MODEL — opencode reads it.
    assert env["OPENCODE_MODEL"] == "openai/flow"


# ---------------------------------------------------------------------------
# Registry — env-driven selection.
# ---------------------------------------------------------------------------


def test_backend_selection_from_env(monkeypatch):
    monkeypatch.setenv("CODE_AGENT_BACKEND", "opencode")
    backend = code_agents.get_backend()
    assert isinstance(backend, OpenCodeBackend)


def test_backend_selection_default_is_aider(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_BACKEND", raising=False)
    backend = code_agents.get_backend()
    assert isinstance(backend, AiderBackend)


def test_backend_selection_explicit_arg_overrides_env(monkeypatch):
    # Tests pass slug directly; that must beat the env var so the
    # registry can be exercised without monkey-patching env.
    monkeypatch.setenv("CODE_AGENT_BACKEND", "opencode")
    backend = code_agents.get_backend("aider")
    assert isinstance(backend, AiderBackend)


def test_unknown_backend_raises_clear_error(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_BACKEND", raising=False)
    with pytest.raises(ValueError) as exc_info:
        code_agents.get_backend("does-not-exist")
    msg = str(exc_info.value)
    assert "does-not-exist" in msg
    # Error must enumerate the registry so the operator can fix the env
    # var without grepping the codebase.
    for slug in code_agents.list_backends():
        assert slug in msg


# ---------------------------------------------------------------------------
# Generic CLI backend — env-driven template, the escape hatch for tools
# we don't have a dedicated backend for yet.
# ---------------------------------------------------------------------------


def test_generic_cli_backend_reads_env_template(monkeypatch):
    # The brief slot must be quoted in the template — shlex.split treats
    # unquoted whitespace as argv boundaries. This is documented in
    # cli_generic.py + tested here so operators copy a working example.
    monkeypatch.setenv(
        "CODE_AGENT_CLI_CMD",
        "my-tool --root {project_dir} --model {model} --prompt '{brief}'",
    )
    monkeypatch.setenv(
        "CODE_AGENT_CLI_ENV",
        "MY_TOOL_KEY={api_key};MY_TOOL_BASE={api_base}/llm/v1",
    )
    backend = GenericCLIBackend()
    argv, env = backend.build_command(
        brief="build a landing for kava",
        project_dir="/workspace/proj-demo",
        api_key="af_live_abc",
        api_base="https://agentflow.website/_agents",
    )
    # Quoted brief preserved as a single argv slot.
    assert "build a landing for kava" in argv
    assert "/workspace/proj-demo" in argv
    assert argv[0] == "my-tool"
    assert env["MY_TOOL_KEY"] == "af_live_abc"
    assert env["MY_TOOL_BASE"] == "https://agentflow.website/_agents/llm/v1"


def test_generic_cli_backend_raises_when_template_unset(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_CLI_CMD", raising=False)
    backend = GenericCLIBackend()
    with pytest.raises(ValueError) as exc_info:
        backend.build_command(
            brief="x", project_dir="/tmp", api_key="k", api_base="https://x"
        )
    assert "CODE_AGENT_CLI_CMD" in str(exc_info.value)


def test_generic_cli_backend_rejects_unknown_slot(monkeypatch):
    # Catching {unknown} at build time is the difference between a clear
    # config error and a silent partial argv that fails opaquely under
    # subprocess.Popen.
    monkeypatch.setenv("CODE_AGENT_CLI_CMD", "my-tool --weird {nope}")
    backend = GenericCLIBackend()
    with pytest.raises(ValueError) as exc_info:
        backend.build_command(
            brief="x", project_dir="/tmp", api_key="k", api_base="https://x"
        )
    assert "nope" in str(exc_info.value)


# ---------------------------------------------------------------------------
# agent_brief.py legacy-kwarg routing — back-compat with tests + server.py
# that still pass ``opencode_bin=`` by name. The new env-var path coexists
# with the kwarg; the kwarg wins so tests stay deterministic.
# ---------------------------------------------------------------------------


def test_resolve_backend_for_spawn_explicit_instance_wins():
    sentinel = AiderBackend(binary="/sentinel/aider")
    resolved = ab._resolve_backend_for_spawn(
        backend=sentinel, aider_bin="aider", opencode_bin=None
    )
    assert resolved is sentinel


def test_resolve_backend_for_spawn_slug_string():
    resolved = ab._resolve_backend_for_spawn(
        backend="opencode", aider_bin="aider", opencode_bin=None
    )
    assert isinstance(resolved, OpenCodeBackend)


def test_resolve_backend_for_spawn_opencode_bin_placeholder_routes_to_aider():
    # The literal ``opencode_bin="opencode"`` was the legacy placeholder
    # default — never actually meant "use OpenCode", just left over from
    # the rename sweep. Preserve that quirk so tests + server.py keep
    # picking aider. Flipping backends must go through ``backend=`` or
    # ``CODE_AGENT_BACKEND``.
    resolved = ab._resolve_backend_for_spawn(
        backend=None, aider_bin="aider", opencode_bin="opencode"
    )
    assert isinstance(resolved, AiderBackend)


def test_resolve_backend_for_spawn_custom_aider_path_via_opencode_bin():
    # Some callers used opencode_bin to override the aider binary path
    # before the renaming sweep landed. Treat any non-"opencode" /
    # non-"aider" string as a custom aider binary path.
    resolved = ab._resolve_backend_for_spawn(
        backend=None, aider_bin="aider", opencode_bin="/custom/path/to/aider"
    )
    assert isinstance(resolved, AiderBackend)
    assert resolved.binary == "/custom/path/to/aider"


def test_resolve_backend_for_spawn_env_var_falls_through(monkeypatch):
    monkeypatch.setenv("CODE_AGENT_BACKEND", "opencode")
    resolved = ab._resolve_backend_for_spawn(
        backend=None, aider_bin="aider", opencode_bin=None
    )
    assert isinstance(resolved, OpenCodeBackend)


def test_resolve_backend_for_spawn_default_aider(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_BACKEND", raising=False)
    resolved = ab._resolve_backend_for_spawn(
        backend=None, aider_bin="aider", opencode_bin=None
    )
    assert isinstance(resolved, AiderBackend)
    assert resolved.binary == "aider"
