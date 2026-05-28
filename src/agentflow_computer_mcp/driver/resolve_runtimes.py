"""On-demand runtime resolver for the hosted-daemon workspace.

Runs AFTER ``git clone`` and BEFORE ``pnpm install`` / coder spawn.
Inspects the project's manifest files (``package.json``, ``requirements.txt``,
``pyproject.toml``, ``Cargo.toml``, ``go.mod``) and ensures the right
toolchain is on PATH inside the pod.

Why: the daemon image bakes one Node version (currently Node 22 after
PR-A) plus a generic Python 3.11. Projects pinning ``engines.node>=24``
or shipping a ``Cargo.toml`` would fall over at ``install`` time with a
cryptic error. This script makes the runtime fit the project, not the
other way around.

Design rules:

* Idempotent — every install step checks the binary version first and
  skips when the constraint is already satisfied. Re-running the script
  on a warm pod is a fast no-op.
* No project-specific code. Only generic manifest inspection.
* Logs are greppable — every action prefixed with ``[resolve-runtimes]``.
* Lazy installs only what the manifest demands. Empty workspace exits 0.
* Time budget: <30s for the common case (Node 22 already there → noop),
  <120s worst case (full Rust toolchain via rustup).
* Side effects (subprocess, HTTP) injectable for tests.

The script is callable two ways:

    python -m agentflow_computer_mcp.driver.resolve_runtimes /workspace/proj-xxx

or from inside ``agent_brief.py``::

    from agentflow_computer_mcp.driver.resolve_runtimes import resolve_runtimes
    resolve_runtimes(project_dir)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LOG_PREFIX = "[resolve-runtimes]"

# Cache for downloaded Node / Go binaries. /opt is writable inside the
# daemon-coder image and survives across task sessions on the same pod.
RUNTIMES_CACHE_DIR = "/opt/runtimes"
N_BINARY_PATH = "/usr/local/bin/n"
N_DOWNLOAD_URL = "https://raw.githubusercontent.com/tj/n/master/bin/n"
RUSTUP_INSTALLER_URL = "https://sh.rustup.rs"
GO_TARBALL_URL_TEMPLATE = "https://go.dev/dl/go{version}.linux-amd64.tar.gz"
DEFAULT_GO_VERSION = "1.22.5"

# Hints from popular packages → required Node major version. Keep
# conservative — only well-known floor versions go here. If a hint is
# missing, ``engines.node`` is the only signal we trust.
_NODE_HINTS: tuple[tuple[str, int], ...] = (
    ("vite", 20),       # vite >=7 requires Node 20.19+
    ("next", 18),       # next >=15 requires Node 18.18+
    ("vitest", 22),     # vitest >=3 requires Node 22
    ("astro", 20),      # astro >=5 requires Node 20.10+
)
# When the hint package's major is >= this trigger, bump Node floor.
_NODE_HINT_PKG_MAJOR_TRIGGER: dict[str, int] = {
    "vite": 7,
    "next": 15,
    "vitest": 3,
    "astro": 5,
}


def _log(msg: str) -> None:
    """Print to stdout with the standard prefix and flush immediately.

    The daemon log-tailer scrapes stdout line-by-line; using print()
    keeps the integration boring. ``flush=True`` so each step is visible
    in real time when the pod log is tailed.
    """
    print(f"{LOG_PREFIX} {msg}", flush=True)


# --- subprocess shim ------------------------------------------------------


def _default_run(
    cmd: Sequence[str],
    cwd: str | None = None,
    *,
    timeout: int = 120,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> dict[str, Any]:
    """Foreground subprocess. Returns ``{exit_code, stdout, stderr}``.

    Mirrors the contract used in ``project_setup._default_run`` so tests
    can swap in the same fake runner.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — args pre-validated
            list(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"exit_code": 127, "stdout": "", "stderr": f"not_found: {exc}"}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '')[:500]}"
        )
    return {
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[:8000],
        "stderr": (proc.stderr or "")[:4000],
    }


def _default_http_download(url: str, dest: str, timeout_s: int = 60) -> bool:
    """Download ``url`` to ``dest``. Returns True on success."""
    try:
        req = urllib.request.Request(
            url, headers={"user-agent": "agentflow-resolve-runtimes/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            data = resp.read()
        Path(dest).write_bytes(data)
        return True
    except (urllib.error.URLError, OSError) as exc:
        _log(f"download_failed {url}: {exc}")
        return False


# --- version helpers ------------------------------------------------------


_SEMVER_RE = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_NODE_CONSTRAINT_RE = re.compile(r"(>=|>|~|\^)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _parse_node_constraint(constraint: str) -> int | None:
    """Extract the required Node major from an ``engines.node`` string.

    Conservatively picks the floor major: ``>=18.18.0`` → 18, ``^20`` → 20,
    ``18.x`` → 18, ``>=20.10.0 <22`` → 20. Returns ``None`` if nothing
    parseable is found. We intentionally ignore upper bounds because the
    daemon installs the floor; installing exact-pinned versions would
    explode the cache.
    """
    if not constraint:
        return None
    m = _NODE_CONSTRAINT_RE.search(constraint)
    if not m:
        return None
    try:
        return int(m.group(2))
    except (TypeError, ValueError):
        return None


def _parse_package_major(version_range: str) -> int | None:
    """Extract major from a package dependency range (``^7.0.0`` → 7)."""
    if not version_range:
        return None
    m = _SEMVER_RE.search(version_range)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _current_node_major(run: Callable[..., dict[str, Any]]) -> int | None:
    """Return the major version of the Node currently on PATH, or None."""
    res = run(["node", "--version"], timeout=10)
    if res.get("exit_code") != 0:
        return None
    out = (res.get("stdout") or "").strip()
    m = _SEMVER_RE.match(out)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


# --- manifest inspection --------------------------------------------------


def required_node_major(project_dir: str) -> int | None:
    """Return the Node major required by the project, or None if unknown.

    Strategy:
      1. ``engines.node`` from ``package.json`` is the authoritative signal.
      2. Fall back to hint packages in ``dependencies`` / ``devDependencies``
         when their major version exceeds the trigger floor.
      3. Return None if no signal — caller leaves Node alone.
    """
    pkg_path = Path(project_dir) / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(pkg, dict):
        return None

    floors: list[int] = []
    engines = pkg.get("engines") or {}
    if isinstance(engines, dict):
        node_eng = engines.get("node")
        if isinstance(node_eng, str):
            engine_floor = _parse_node_constraint(node_eng)
            if engine_floor is not None:
                floors.append(engine_floor)

    all_deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        chunk = pkg.get(key)
        if isinstance(chunk, dict):
            all_deps.update(chunk)

    for hint_pkg, hint_floor in _NODE_HINTS:
        ver = all_deps.get(hint_pkg)
        if not isinstance(ver, str):
            continue
        pkg_major = _parse_package_major(ver)
        trigger = _NODE_HINT_PKG_MAJOR_TRIGGER.get(hint_pkg)
        if pkg_major is None or trigger is None:
            continue
        if pkg_major >= trigger:
            floors.append(hint_floor)

    if not floors:
        return None
    return max(floors)


def has_python_project(project_dir: str) -> bool:
    """True if the project ships a Python manifest the resolver handles."""
    base = Path(project_dir)
    return (base / "requirements.txt").is_file() or (base / "pyproject.toml").is_file()


def has_rust_project(project_dir: str) -> bool:
    return (Path(project_dir) / "Cargo.toml").is_file()


def has_go_project(project_dir: str) -> bool:
    return (Path(project_dir) / "go.mod").is_file()


# --- install steps --------------------------------------------------------


def _ensure_n_installed(
    run: Callable[..., dict[str, Any]],
    http_download: Callable[[str, str, int], bool],
) -> bool:
    """Make sure tj/n is on PATH. Lazy-installs to /usr/local/bin/n.

    n is a single bash script (~300 lines) so we curl it directly rather
    than reaching for npm. Cached after first install for the pod's life.
    """
    if Path(N_BINARY_PATH).is_file() and os.access(N_BINARY_PATH, os.X_OK):
        return True
    _log(f"installing n into {N_BINARY_PATH}")
    ok = http_download(N_DOWNLOAD_URL, N_BINARY_PATH, 30)
    if not ok:
        return False
    try:
        os.chmod(N_BINARY_PATH, 0o755)
    except OSError as exc:
        _log(f"chmod_failed {N_BINARY_PATH}: {exc}")
        return False
    return True


def install_node(
    required_major: int,
    *,
    run: Callable[..., dict[str, Any]] = _default_run,
    http_download: Callable[[str, str, int], bool] = _default_http_download,
    current_major: Callable[[Callable[..., dict[str, Any]]], int | None] = _current_node_major,
) -> dict[str, Any]:
    """Ensure ``node --version`` major is ``>= required_major``.

    Returns ``{action: "skip"|"install"|"fail", ...}``. Idempotent.
    """
    current = current_major(run)
    if current is not None and current >= required_major:
        _log(f"node ok: have v{current}, need >= {required_major}, skipping install")
        return {"action": "skip", "current_major": current, "required_major": required_major}

    _log(
        f"node mismatch: have "
        f"{'none' if current is None else f'v{current}'}, "
        f"need >= {required_major}; installing via n"
    )

    try:
        Path(RUNTIMES_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"cache_dir_unwritable {RUNTIMES_CACHE_DIR}: {exc}")
        # Non-fatal: n falls back to /usr/local without the cache dir.

    if not _ensure_n_installed(run, http_download):
        return {"action": "fail", "error": "n_install_failed"}

    env = {
        **os.environ,
        # N_PREFIX is where `n` installs node binaries. Default is
        # /usr/local which conflicts with the image's baked node; using
        # a dedicated dir keeps both versions around for rollback.
        "N_PREFIX": RUNTIMES_CACHE_DIR,
    }
    install_res = run(
        [N_BINARY_PATH, "install", str(required_major)],
        timeout=180,
        env=env,
    )
    if install_res.get("exit_code") != 0:
        detail = (install_res.get("stderr") or install_res.get("stdout") or "")[:500]
        _log(f"n install failed: {detail}")
        return {"action": "fail", "error": "n_install_failed", "detail": detail}

    # Symlink the new node binary onto PATH ahead of the image's default.
    new_bin = Path(RUNTIMES_CACHE_DIR) / "bin" / "node"
    target = Path("/usr/local/bin/node")
    if new_bin.is_file():
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(new_bin)
            _log(f"node {required_major} symlinked: {target} -> {new_bin}")
        except OSError as exc:
            _log(f"symlink_failed: {exc}")
            return {"action": "fail", "error": "symlink_failed", "detail": str(exc)}

    return {"action": "install", "required_major": required_major}


def install_python_deps(
    project_dir: str,
    *,
    run: Callable[..., dict[str, Any]] = _default_run,
) -> dict[str, Any]:
    """Create ``.venv/`` inside workspace and install Python deps.

    Idempotent: if ``.venv/bin/python`` exists, skips creation. Uses
    ``uv`` when present (10x faster than pip) and falls back to
    ``python -m venv`` + ``pip``.
    """
    base = Path(project_dir)
    req = base / "requirements.txt"
    pyproject = base / "pyproject.toml"
    if not req.is_file() and not pyproject.is_file():
        return {"action": "skip", "reason": "no_python_manifest"}

    venv_dir = base / ".venv"
    venv_python = venv_dir / "bin" / "python"

    if not venv_python.is_file():
        _log(f"creating venv at {venv_dir}")
        # uv venv if available; else python -m venv.
        if shutil.which("uv"):
            res = run(["uv", "venv", str(venv_dir)], cwd=project_dir, timeout=60)
        else:
            res = run([sys.executable, "-m", "venv", str(venv_dir)], cwd=project_dir, timeout=60)
        if res.get("exit_code") != 0:
            detail = (res.get("stderr") or res.get("stdout") or "")[:500]
            _log(f"venv_create_failed: {detail}")
            return {"action": "fail", "error": "venv_create_failed", "detail": detail}
    else:
        _log(f"venv already exists at {venv_dir}, reusing")

    pip = str(venv_dir / "bin" / "pip")
    if req.is_file():
        _log(f"pip install -r {req.name}")
        res = run([pip, "install", "-r", str(req)], cwd=project_dir, timeout=300)
        if res.get("exit_code") != 0:
            detail = (res.get("stderr") or res.get("stdout") or "")[:500]
            _log(f"pip_install_failed: {detail}")
            return {"action": "fail", "error": "pip_install_failed", "detail": detail}
    elif pyproject.is_file():
        _log("pip install -e . (from pyproject.toml)")
        res = run([pip, "install", "-e", "."], cwd=project_dir, timeout=300)
        if res.get("exit_code") != 0:
            detail = (res.get("stderr") or res.get("stdout") or "")[:500]
            _log(f"pip_install_e_failed: {detail}")
            return {"action": "fail", "error": "pip_install_e_failed", "detail": detail}

    return {"action": "install", "venv": str(venv_dir)}


def install_rust(
    project_dir: str,
    *,
    run: Callable[..., dict[str, Any]] = _default_run,
    http_download: Callable[[str, str, int], bool] = _default_http_download,
) -> dict[str, Any]:
    """Ensure rustup + cargo are on PATH; run ``cargo fetch``.

    rustup-init is downloaded to /tmp and invoked with ``-y --default-toolchain stable``
    when ``cargo`` is missing. Subsequent runs are skipped.
    """
    cargo_path = shutil.which("cargo")
    if not cargo_path:
        # Lazy install via the official rustup-init shell script.
        installer = "/tmp/rustup-init.sh"
        _log("cargo not found; installing rustup")
        if not http_download(RUSTUP_INSTALLER_URL, installer, 60):
            return {"action": "fail", "error": "rustup_download_failed"}
        try:
            os.chmod(installer, 0o755)
        except OSError as exc:
            return {"action": "fail", "error": "chmod_failed", "detail": str(exc)}
        env = {
            **os.environ,
            "RUSTUP_HOME": f"{RUNTIMES_CACHE_DIR}/rustup",
            "CARGO_HOME": f"{RUNTIMES_CACHE_DIR}/cargo",
        }
        res = run(
            [installer, "-y", "--default-toolchain", "stable", "--no-modify-path"],
            timeout=120,
            env=env,
        )
        if res.get("exit_code") != 0:
            detail = (res.get("stderr") or res.get("stdout") or "")[:500]
            return {"action": "fail", "error": "rustup_install_failed", "detail": detail}
        # Symlink cargo + rustc onto PATH.
        for tool in ("cargo", "rustc"):
            src = Path(env["CARGO_HOME"]) / "bin" / tool
            dst = Path("/usr/local/bin") / tool
            if src.is_file():
                try:
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                    dst.symlink_to(src)
                except OSError as exc:
                    _log(f"symlink_failed {tool}: {exc}")
        cargo_path = shutil.which("cargo") or str(Path(env["CARGO_HOME"]) / "bin" / "cargo")
    else:
        _log(f"cargo already present at {cargo_path}, skipping rustup")

    _log("cargo fetch")
    fetch_res = run([cargo_path, "fetch"], cwd=project_dir, timeout=300)
    if fetch_res.get("exit_code") != 0:
        detail = (fetch_res.get("stderr") or fetch_res.get("stdout") or "")[:500]
        _log(f"cargo_fetch_failed: {detail}")
        return {"action": "fail", "error": "cargo_fetch_failed", "detail": detail}
    return {"action": "install", "cargo": cargo_path}


def install_go(
    project_dir: str,
    *,
    run: Callable[..., dict[str, Any]] = _default_run,
    http_download: Callable[[str, str, int], bool] = _default_http_download,
    version: str = DEFAULT_GO_VERSION,
) -> dict[str, Any]:
    """Ensure ``go`` is on PATH; run ``go mod download``.

    Pulls the official linux-amd64 tarball when missing. ARM pods need
    a different URL — out of scope; pod manifests stay amd64 today.
    """
    go_path = shutil.which("go")
    if not go_path:
        _log(f"go not found; installing go {version}")
        tarball = f"/tmp/go-{version}.tar.gz"
        url = GO_TARBALL_URL_TEMPLATE.format(version=version)
        if not http_download(url, tarball, 120):
            return {"action": "fail", "error": "go_download_failed"}
        target_dir = Path(RUNTIMES_CACHE_DIR) / "go"
        try:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"action": "fail", "error": "go_dir_failed", "detail": str(exc)}
        # tar -C parent -xzf tarball strips the leading "go/" dir into target_dir.
        extract_res = run(
            ["tar", "-C", str(target_dir.parent), "-xzf", tarball],
            timeout=120,
        )
        if extract_res.get("exit_code") != 0:
            detail = (extract_res.get("stderr") or extract_res.get("stdout") or "")[:500]
            return {"action": "fail", "error": "go_extract_failed", "detail": detail}
        # Symlink go binary onto PATH.
        src = target_dir / "bin" / "go"
        dst = Path("/usr/local/bin/go")
        if src.is_file():
            try:
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(src)
            except OSError as exc:
                _log(f"symlink_failed go: {exc}")
        go_path = shutil.which("go") or str(src)
    else:
        _log(f"go already present at {go_path}, skipping install")

    _log("go mod download")
    res = run([go_path, "mod", "download"], cwd=project_dir, timeout=180)
    if res.get("exit_code") != 0:
        detail = (res.get("stderr") or res.get("stdout") or "")[:500]
        _log(f"go_mod_download_failed: {detail}")
        return {"action": "fail", "error": "go_mod_download_failed", "detail": detail}
    return {"action": "install", "go": go_path}


# --- top-level orchestrator -----------------------------------------------


# ---------------------------------------------------------------------------
# .env.example → .env.local with dev-safe dummies
# ---------------------------------------------------------------------------
# Why: templates from the wider ecosystem (Clerk-based Next.js, Supabase
# starters, NextAuth examples) crash on first render when their required
# env vars are missing. the coder agent could in principle copy `.env.example`
# itself, but historically refuses to invent secrets (security guardrail),
# leaving the dev server to 500 on first request. We pre-seed safe dev
# defaults BEFORE the coder agent runs so the template boots; the brief tells the
# user (and the coder agent) that real values can be set in
# /cabinet/devices/<id>/secrets later.

# Heuristics: longest-match key suffix wins.
_ENV_DEFAULTS: tuple[tuple[str, str], ...] = (
    # Clerk / Stripe-style publishable keys — start with `pk_` and are
    # safe to ship as literals in the browser bundle.
    ("PUBLISHABLE_KEY", "pk_test_dummy_dev_value_replace_in_cabinet"),
    # Generic API/secret keys → 32 hex chars (deterministic dummy so
    # the same workspace boots the same on repeat clones).
    ("SECRET_KEY", "dummydummydummydummydummydummy00"),
    ("SECRET", "dummydummydummydummydummydummy00"),
    ("API_KEY", "dummydummydummydummydummydummy00"),
    ("ACCESS_KEY", "dummydummydummydummydummydummy00"),
    # Auth/session bits.
    ("NEXTAUTH_SECRET", "dummydummydummydummydummydummy00"),
    ("NEXTAUTH_URL", "http://localhost:3000"),
    ("AUTH_SECRET", "dummydummydummydummydummydummy00"),
    ("JWT_SECRET", "dummydummydummydummydummydummy00"),
    # Database URLs — sqlite is the universal happy-path: works with
    # prisma, drizzle, sqlalchemy, sequelize out of the box when the
    # driver is installed.
    ("DATABASE_URL", "file:./dev.db"),
    ("DIRECT_URL", "file:./dev.db"),
    ("POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/postgres"),
    ("REDIS_URL", "redis://localhost:6379"),
    ("MONGODB_URI", "mongodb://localhost:27017/dev"),
    # Common URL bases.
    ("BASE_URL", "http://localhost:3000"),
    ("APP_URL", "http://localhost:3000"),
    ("PUBLIC_URL", "http://localhost:3000"),
    ("FRONTEND_URL", "http://localhost:3000"),
    ("API_URL", "http://localhost:3000"),
    ("URL", "http://localhost:3000"),
    # Storage / S3 — `minio` defaults match most local-S3 starters.
    ("S3_ACCESS_KEY_ID", "minioadmin"),
    ("S3_SECRET_ACCESS_KEY", "minioadmin"),
    ("S3_BUCKET", "dev-bucket"),
    ("S3_REGION", "us-east-1"),
    # Email — disable so dev doesn't try to send.
    ("SMTP_HOST", "localhost"),
    ("SMTP_PORT", "1025"),
    ("EMAIL_FROM", "dev@localhost"),
    # OAuth — empty client IDs trigger most templates' "auth disabled"
    # fallback path rather than crashing on undefined.
    ("CLIENT_ID", ""),
    ("CLIENT_SECRET", ""),
    # NEXT_PUBLIC_* generic — public values, safe dummies.
    ("ENABLE", "false"),
)


def _pick_env_default(key: str) -> str:
    """Pick a dev-safe default for an env var by longest-matching suffix."""
    upper = key.upper()
    for suffix, value in _ENV_DEFAULTS:
        if upper == suffix or upper.endswith("_" + suffix) or upper.endswith(suffix):
            return value
    # Fallback: empty string. Templates that crash on empty are rare and
    # we cannot guess intent without a spec — better to keep the crash
    # visible than to silently shove a junk value.
    return ""


def _parse_env_example(text: str) -> list[tuple[str, str]]:
    """Parse a dotenv-shaped file into `[(key, sample_value), ...]`.

    Comments and blanks are skipped. Right-hand side preserved verbatim
    so the caller can decide whether to overwrite (we only fill keys
    whose existing value is empty / sample-looking).
    """
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        out.append((key, value.strip()))
    return out


_ENV_TARGET_NAMES: tuple[str, ...] = (".env.local", ".env")


def ensure_env_defaults(project_dir: str) -> dict[str, Any]:
    """Seed dev-safe dummies into ``.env.local`` (or ``.env``) for any
    key listed in ``.env.example`` that isn't already populated.

    Idempotent: re-running never overwrites a key that already has a
    non-empty, non-sample value. The output dict reports which keys
    were filled so the daemon log shows it for grep.
    """
    if not project_dir or not Path(project_dir).is_dir():
        return {"action": "skip", "reason": "workspace_missing"}

    example_path: Path | None = None
    for name in (".env.example", ".env.sample", ".env.template"):
        p = Path(project_dir) / name
        if p.is_file():
            example_path = p
            break
    if example_path is None:
        _log("env: no .env.example / .env.sample, skipping")
        return {"action": "skip", "reason": "no_env_example"}

    try:
        example_text = example_path.read_text(encoding="utf-8")
    except OSError as exc:
        _log(f"env: could not read {example_path}: {exc}")
        return {"action": "skip", "reason": "read_failed"}

    wanted = _parse_env_example(example_text)
    if not wanted:
        _log("env: .env.example parsed empty, skipping")
        return {"action": "skip", "reason": "empty"}

    # Prefer .env.local (Next.js convention; never committed); fall back
    # to .env (Prisma / fastify / nest).
    target = Path(project_dir) / _ENV_TARGET_NAMES[0]
    existing: dict[str, str] = {}
    if target.exists():
        try:
            for k, v in _parse_env_example(target.read_text(encoding="utf-8")):
                existing[k] = v
        except (OSError, UnicodeDecodeError):
            existing = {}

    filled: list[str] = []
    lines = [
        "# Auto-generated by AgentFlow daemon. Replace in /cabinet/devices/<id>/secrets",
        "# to plug real provider keys. Safe to commit nothing — this file should be",
        "# gitignored by every template that uses .env.example.",
        "",
    ]
    for key, sample in wanted:
        if existing.get(key, "").strip():
            lines.append(f"{key}={existing[key]}")
            continue
        # Sample values that look real (e.g. "https://example.com/v1") are
        # often documentation, not dummies — pick our own default and
        # ignore the sample.
        chosen = _pick_env_default(key)
        if not chosen and sample and not sample.startswith(("<", "your-", "your_", "xxx")):
            # The .env.example author left a sane default — keep it.
            chosen = sample.strip().strip("\"'")
        if not chosen:
            # Leave as empty; some templates branch on falsy.
            lines.append(f"{key}=")
            continue
        lines.append(f"{key}={chosen}")
        filled.append(key)

    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        _log(f"env: write failed {target}: {exc}")
        return {"action": "fail", "reason": "write_failed", "detail": str(exc)}

    _log(f"env: wrote {target.name} ({len(filled)} dummies, {len(wanted)} total)")
    return {
        "action": "wrote",
        "path": str(target),
        "filled_keys": filled,
        "total_keys": len(wanted),
    }


def resolve_runtimes(
    project_dir: str,
    *,
    run: Callable[..., dict[str, Any]] = _default_run,
    http_download: Callable[[str, str, int], bool] = _default_http_download,
    current_node: Callable[[Callable[..., dict[str, Any]]], int | None] = _current_node_major,
) -> dict[str, Any]:
    """Inspect ``project_dir`` and ensure every needed runtime is ready.

    Order:
      1. Node (cheapest check, most common project type)
      2. Python venv + deps
      3. Rust toolchain (longest install — only if Cargo.toml)
      4. Go (only if go.mod)

    Returns a summary dict for the caller's logs / reports. Never raises
    on missing manifests; an empty workspace returns
    ``{ok: True, actions: []}``.
    """
    if not project_dir or not Path(project_dir).is_dir():
        _log(f"workspace_missing: {project_dir!r}")
        return {"ok": False, "error": "workspace_missing", "actions": []}

    _log(f"start workspace={project_dir}")
    actions: list[dict[str, Any]] = []

    # 0. Env defaults — seed .env.local from .env.example BEFORE the coder agent
    # runs so dev servers don't crash on missing `NEXT_PUBLIC_CLERK_*`,
    # `DATABASE_URL`, etc. Idempotent and conservative — only fills empty
    # keys with safe dummies; never overrides a real value the user
    # already pasted in.
    env_result = ensure_env_defaults(project_dir)
    actions.append({"runtime": "env", **env_result})

    # 1. Node.
    needed_node = required_node_major(project_dir)
    if needed_node is not None:
        result = install_node(
            needed_node,
            run=run,
            http_download=http_download,
            current_major=current_node,
        )
        actions.append({"runtime": "node", **result})
    else:
        _log("node: no package.json or no version signal, skipping")

    # 2. Python.
    if has_python_project(project_dir):
        result = install_python_deps(project_dir, run=run)
        actions.append({"runtime": "python", **result})
    else:
        _log("python: no requirements.txt / pyproject.toml, skipping")

    # 3. Rust.
    if has_rust_project(project_dir):
        result = install_rust(project_dir, run=run, http_download=http_download)
        actions.append({"runtime": "rust", **result})
    else:
        _log("rust: no Cargo.toml, skipping")

    # 4. Go.
    if has_go_project(project_dir):
        result = install_go(project_dir, run=run, http_download=http_download)
        actions.append({"runtime": "go", **result})
    else:
        _log("go: no go.mod, skipping")

    ok = all(a.get("action") != "fail" for a in actions)
    _log(f"done ok={ok} actions={len(actions)}")
    return {"ok": ok, "actions": actions, "project_dir": project_dir}


def _cli(argv: list[str]) -> int:
    """Entrypoint for ``python -m agentflow_computer_mcp.driver.resolve_runtimes``."""
    if not argv:
        print(
            f"{LOG_PREFIX} usage: python -m agentflow_computer_mcp.driver.resolve_runtimes <workspace>",
            file=sys.stderr,
        )
        return 2
    workspace = argv[0]
    result = resolve_runtimes(workspace)
    # Exit 0 even on failure if it's just a missing optional toolchain;
    # caller (agent_brief) decides whether to abort. Hard fail only when
    # the workspace itself is missing.
    if result.get("error") == "workspace_missing":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — entrypoint
    sys.exit(_cli(sys.argv[1:]))
