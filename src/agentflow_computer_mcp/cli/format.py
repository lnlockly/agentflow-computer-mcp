"""Output helpers for the CLI: key masking, simple tables, currency."""
from __future__ import annotations


def mask_key(key: str) -> str:
    """Show first 8 chars + ellipsis. Never echo the whole key."""
    if not key:
        return "(none)"
    if len(key) <= 8:
        return key[:2] + "..."
    return key[:8] + "..."


def table(rows: list[dict[str, object]], columns: list[str]) -> str:
    """Render rows as a fixed-width ASCII table. No external deps."""
    if not rows:
        return "(empty)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    body = "\n".join("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows)
    return f"{header}\n{sep}\n{body}"


def fmt_money(cents_or_usd: float, *, as_dollars: bool = True) -> str:
    value = cents_or_usd if as_dollars else cents_or_usd / 100.0
    return f"${value:.2f}"


def fmt_budget(spent: float, cap: float) -> str:
    pct = (spent / cap * 100.0) if cap > 0 else 0.0
    return f"{fmt_money(spent)} / {fmt_money(cap)} ({pct:.0f}%)"
