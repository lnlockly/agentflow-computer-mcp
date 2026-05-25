"""Install-wizard step contract — declarative manifest loader + runner.

Single source of truth: `installer/steps.json` in this repo. The Tk
wizard (`setup_gui.py`) and the React cabinet wizard
(`agentflow-landing/src/components/InstallWizard.tsx`) both consume the
same shape so the user sees identical row labels across surfaces.

Usage:

    from installer.steps import load_steps, StepRunner

    runner = StepRunner(
        steps=load_steps(surface="win"),
        progress=lambda name, status, detail="": print(f"STEP {name} {status} {detail}"),
    )
    runner.register("prepare_workspace", lambda ctx: ctx["workspace"].mkdir(exist_ok=True))
    runner.register("verify_token", lambda ctx: mint_device_via_api(ctx["token"]))
    # ...
    result = runner.run(context={"workspace": Path.home() / ".agentflow", "token": "af_live_..."})

Steps with `status: "planned"` are surfaced to the UI but their `fn` is
never called — the runner skips them with `status="skipped_planned"`
progress event. Unknown step names registered against the runner raise
`UnknownStepError` so typos fail loud.

This module deliberately has zero dependencies outside the stdlib so it
can be imported by the PyInstaller-frozen wizard without bloating the
binary.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SUPPORTED_SCHEMA_VERSION = 1

Surface = Literal["win", "mac", "linux", "hosted"]
StepStatusManifest = Literal["real", "planned"]
StepStatusRuntime = Literal[
    "wait",
    "running",
    "ok",
    "error",
    "skipped_surface",
    "skipped_planned",
]

ProgressCallback = Callable[[str, StepStatusRuntime, str], None]


class StepsManifestError(RuntimeError):
    """Raised when steps.json is malformed or schema version unsupported."""


class UnknownStepError(RuntimeError):
    """Raised when a Step name is registered or run that isn't in the manifest."""


@dataclass(frozen=True)
class Step:
    """One row in the install wizard.

    `fn` is set later via `StepRunner.register`; the manifest only
    describes the row, not the work. This keeps the JSON portable to
    surfaces that don't run Python at all (the React UI).
    """

    name: str
    label_ru: str
    label_en: str
    surfaces: tuple[Surface, ...]
    required: bool
    status: StepStatusManifest
    notes: Optional[str] = None
    issue: Optional[str] = None
    unblocks_with: Optional[str] = None

    def applies_to(self, surface: Surface) -> bool:
        return surface in self.surfaces


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _default_steps_path() -> Path:
    """`installer/steps.json` co-located with this module."""
    return Path(__file__).resolve().parent / "steps.json"


def load_steps(
    surface: Optional[Surface] = None,
    *,
    path: Optional[Path] = None,
) -> list[Step]:
    """Parse the manifest and return Step objects.

    If `surface` is provided, only steps that apply to that surface are
    returned (preserves manifest order). Otherwise returns every step.

    Raises `StepsManifestError` on missing file, malformed JSON, schema
    version drift, or missing required fields.
    """
    src = path or _default_steps_path()
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise StepsManifestError(f"steps manifest not found at {src}") from e
    except json.JSONDecodeError as e:
        raise StepsManifestError(f"steps manifest is not valid JSON: {e}") from e

    version = raw.get("version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise StepsManifestError(
            f"unsupported steps.json schema version {version!r} "
            f"(this runner expects v{SUPPORTED_SCHEMA_VERSION}); "
            f"update installer/steps.py before bumping the manifest"
        )

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise StepsManifestError("steps manifest must have a non-empty `steps` array")

    out: list[Step] = []
    for i, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise StepsManifestError(f"step #{i} is not an object")
        try:
            step = Step(
                name=str(item["name"]),
                label_ru=str(item["label_ru"]),
                label_en=str(item["label_en"]),
                surfaces=tuple(item["surfaces"]),
                required=bool(item["required"]),
                status=item["status"],
                notes=item.get("notes"),
                issue=item.get("issue"),
                unblocks_with=item.get("unblocks_with"),
            )
        except KeyError as e:
            raise StepsManifestError(f"step #{i} missing required field {e}") from e

        if step.status not in ("real", "planned"):
            raise StepsManifestError(
                f"step {step.name!r} has invalid status {step.status!r} "
                f"(must be 'real' or 'planned')"
            )
        out.append(step)

    if surface is not None:
        out = [s for s in out if s.applies_to(surface)]
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _noop_progress(_name: str, _status: StepStatusRuntime, _detail: str) -> None:
    return None


@dataclass
class StepResult:
    name: str
    status: StepStatusRuntime
    detail: str = ""
    elapsed_sec: float = 0.0


@dataclass
class StepRunner:
    """Executes registered callbacks for each Step in manifest order.

    Each callback gets a shared `context` dict (you populate it before
    `run()`) and returns either `None` / `True` (= ok) or a string
    detail to surface in the progress event. Raising any exception is
    treated as `status='error'` with the exception message as detail.

    `planned` steps are never invoked — the runner emits a
    `skipped_planned` progress event so the UI can show "скоро" without
    the row going stale.

    Steps that don't apply to the current `surface` are skipped with
    `skipped_surface` (still emitted so the UI keeps row alignment
    across surfaces if it cares).
    """

    steps: list[Step]
    progress: ProgressCallback = field(default=_noop_progress)
    surface: Optional[Surface] = None
    _registry: dict[str, Callable[[dict[str, Any]], Any]] = field(default_factory=dict, init=False)

    def register(self, name: str, fn: Callable[[dict[str, Any]], Any]) -> None:
        """Bind a callback to a step name. Must be present in the manifest."""
        if not any(s.name == name for s in self.steps):
            raise UnknownStepError(
                f"step {name!r} not in manifest — typo or surface filter dropped it?"
            )
        self._registry[name] = fn

    def known_steps(self) -> Iterable[str]:
        return [s.name for s in self.steps]

    def run(self, context: Optional[dict[str, Any]] = None) -> list[StepResult]:
        """Execute every step in manifest order. Returns ordered results.

        Stops on first `required=True` error to mirror the user's
        expectation that they can't proceed past a hard failure.
        Optional-step errors continue but are surfaced.
        """
        ctx = context or {}
        results: list[StepResult] = []
        for step in self.steps:
            if self.surface is not None and not step.applies_to(self.surface):
                self.progress(step.name, "skipped_surface", "")
                results.append(StepResult(step.name, "skipped_surface"))
                continue
            if step.status == "planned":
                self.progress(step.name, "skipped_planned", step.unblocks_with or "")
                results.append(StepResult(step.name, "skipped_planned", step.unblocks_with or ""))
                continue
            fn = self._registry.get(step.name)
            if fn is None:
                # No callback registered for a real step → record as error
                # so the integrator notices missing wiring instead of
                # silent green.
                detail = f"no callback registered for {step.name!r}"
                self.progress(step.name, "error", detail)
                results.append(StepResult(step.name, "error", detail))
                if step.required:
                    break
                continue

            self.progress(step.name, "running", "")
            t0 = time.monotonic()
            try:
                out = fn(ctx)
                elapsed = time.monotonic() - t0
                detail = "" if out is None or out is True else str(out)
                self.progress(step.name, "ok", detail)
                results.append(StepResult(step.name, "ok", detail, elapsed))
            except Exception as e:  # noqa: BLE001 — runner deliberately catches all
                elapsed = time.monotonic() - t0
                detail = f"{type(e).__name__}: {e}"
                self.progress(step.name, "error", detail)
                results.append(StepResult(step.name, "error", detail, elapsed))
                if step.required:
                    break
        return results


__all__ = [
    "Step",
    "StepResult",
    "StepRunner",
    "StepsManifestError",
    "UnknownStepError",
    "SUPPORTED_SCHEMA_VERSION",
    "load_steps",
]
