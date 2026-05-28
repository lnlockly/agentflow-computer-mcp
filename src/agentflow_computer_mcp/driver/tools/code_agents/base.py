"""Abstract code-agent backend contract.

The hosted daemon's project-bootstrap path used to hardcode a single
``aider`` invocation in :mod:`agent_brief`. That worked while aider was
the only viable option, but a stuck gateway / new tool / vendor outage
turned every swap into a code change + image rebuild + redeploy. Owner
asked for a strategy pattern so the binary in the loop is **runtime**
configurable.

Concretely: ``agent_brief`` asks :func:`code_agents.get_backend` for a
:class:`CodeAgentBackend` and calls :meth:`build_command` to obtain the
exact argv + env mutations for :class:`subprocess.Popen`. Adding a new
tool (Continue, Goose, Cline) is a new file in this package + one
registry line, no surgery on agent_brief.

Design constraints baked into the ABC:

* **Pure builders.** ``build_command`` must not touch disk or network.
  It returns ``(argv, env_overrides)`` for the caller's Popen. Side
  effects belong in agent_brief (workspace prep, .env writes, watchers).
* **Env-overrides are additive.** The returned dict is layered onto
  the caller's env, never replaces it. Backends only override what
  matters to them (eg ``OPENAI_API_BASE`` for aider's OpenAI shim).
* **Healthcheck is best-effort.** ``healthcheck`` returns ``True`` when
  the backend's CLI is on ``PATH`` and reports a version. Used by
  diagnostics — not in the hot spawn path so a flaky binary check never
  blocks a project.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class CodeAgentBackend(ABC):
    """Contract every code-editing backend implements.

    Subclasses must set :attr:`slug` to a unique lowercase identifier —
    this is what :envvar:`CODE_AGENT_BACKEND` matches against. The
    registry in ``__init__.py`` keys off the slug; duplicates raise at
    import time.
    """

    #: Unique lowercase backend identifier (``aider`` / ``opencode`` / ``cli``).
    slug: str = ""

    @abstractmethod
    def build_command(
        self,
        *,
        brief: str,
        project_dir: str,
        api_key: str,
        api_base: str,
        model: str = "openai/flow",
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(argv, env_overrides)`` for ``subprocess.Popen``.

        Parameters
        ----------
        brief
            The composed user brief, already wrapped with the daemon's
            instruction shell. Backends pass it through verbatim — they
            do not re-edit the prompt.
        project_dir
            Absolute path to the workspace the backend will edit. Used
            by backends that take the project root as an explicit arg
            (some CLIs do, aider infers it from ``cwd``).
        api_key
            AgentFlow gateway key (``AF_API_KEY``). Empty string when the
            daemon is running without owner-key injection, in which case
            the backend must still produce a runnable argv — the spawn
            will fail loudly later if the model needs auth.
        api_base
            Already-normalised gateway base (eg ``https://agentflow.website/_agents``).
            Backends that target an OpenAI-shaped endpoint append
            ``/llm/v1`` themselves.
        model
            Logical model alias resolved server-side. Defaults to
            ``openai/flow`` (AgentFlow's flow alias). Backends MAY ignore
            the value when their CLI doesn't accept a model flag.

        Returns
        -------
        tuple[list[str], dict[str, str]]
            * ``argv`` — first element is the binary; remaining are flags
              + the brief. Caller wires it straight into ``Popen``.
            * ``env_overrides`` — keys to merge on top of the caller's
              environment dict. Empty dict is fine.
        """

    @abstractmethod
    def healthcheck(self) -> bool:
        """Return ``True`` iff the CLI binary is discoverable on ``PATH``.

        Implementations should not raise — log + return False on
        failure. The daemon may call this during boot to surface a clear
        ``backend_unhealthy`` error before the first project runs.
        """
