"""Common helpers shared across the bot package.

This module centralises the small type-coercion and formatting helpers that
were historically duplicated as private functions in many sibling modules
(``_float``, ``_dict``, ``_list``, ``_fmt``, ``_pct``, ``_int_or_none``,
``_float_or_none`` ...). Behaviour is preserved exactly; the legacy modules
keep their private names as aliases imported from here.

Naming convention:

* ``as_<type>``         — strict coercion; returns ``None`` for unrecognised input.
* ``as_<type>_lax``     — accepts strings; returns ``None`` only when parsing fails.
* ``as_<type>_or``      — returns a caller-supplied default instead of ``None``.
* ``fmt_<kind>``        — human-readable string formatting with a sentinel for
                          missing / non-numeric input (matches the legacy
                          ``"none"`` text used by Markdown reports).

Also exposes :func:`setup_logging`, the single source of truth for logging
configuration. The CLI entry point (``bot.main``) calls it once on startup;
without that call, the few modules that use ``logging.getLogger`` would emit
nothing because the root logger defaults to ``WARNING`` with no handlers.
"""

from __future__ import annotations

import logging
import os
from typing import Any

__all__ = [
    "as_float",
    "as_float_lax",
    "as_float_or",
    "as_int",
    "as_dict",
    "as_list",
    "as_list_of_dicts",
    "as_str_list",
    "fmt_number",
    "fmt_percent",
    "setup_logging",
]


# ---------------------------------------------------------------------------
# Numeric coercion
# ---------------------------------------------------------------------------

def as_float(value: object) -> float | None:
    """Return ``value`` as a float when it is already ``int`` or ``float``.

    Strict by design: strings and other types yield ``None``. Use
    :func:`as_float_lax` if you need permissive parsing.

    Note: ``bool`` is a subclass of ``int`` in Python and is accepted here for
    parity with the legacy ``_float`` / ``_float_or_none`` / ``_float_value``
    helpers this function replaces.
    """
    if isinstance(value, (int, float)):
        return float(value)
    return None


def as_float_lax(value: object) -> float | None:
    """Return ``value`` as a float, including string parsing.

    Returns ``None`` for ``None``, empty strings, or values that cannot be
    parsed by :func:`float`.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def as_float_or(value: object, default: float = 0.0) -> float:
    """Permissive ``float`` coercion with a non-``None`` fallback.

    Equivalent to :func:`as_float_lax` but substitutes ``default`` when the
    input cannot be coerced. Used where downstream callers expect a numeric
    value (e.g. accumulators in the historical fetcher).
    """
    parsed = as_float_lax(value)
    return default if parsed is None else parsed


def as_int(value: object) -> int | None:
    """Return ``value`` as ``int`` when it is an ``int`` (or ``bool``), else ``None``.

    ``bool`` passes the ``isinstance(value, int)`` check (it is a subclass) for
    parity with the legacy ``_int_or_none`` helper. Floats are rejected; use a
    cast at the call site if rounding is desired.
    """
    if isinstance(value, int):
        return value
    return None


# ---------------------------------------------------------------------------
# Container coercion
# ---------------------------------------------------------------------------

def as_dict(value: object) -> dict[str, Any]:
    """Return ``value`` if it is a dict, otherwise an empty dict.

    Note: keys are not coerced to ``str``; the return type is annotated for
    convenience but reflects the legacy ``dict[str, object]`` usage.
    """
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    """Return ``value`` if it is a list, otherwise an empty list."""
    return list(value) if isinstance(value, list) else []


def as_list_of_dicts(value: object) -> list[dict[str, Any]]:
    """Return only the dict items from ``value`` (or empty if not a list).

    Stricter than :func:`as_list`: non-dict entries are silently dropped. This
    matches the safer convention used by ``backtest_reporting`` and
    ``settlement_validation`` and is the preferred form when iterating
    report rows.
    """
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def as_str_list(value: object) -> list[str]:
    """Return a list of strings, coercing each element."""
    return [str(item) for item in value] if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------

def fmt_number(value: object, *, digits: int = 4, none_text: str = "none") -> str:
    """Format a numeric value with fixed decimal places.

    Returns ``none_text`` when ``value`` is not numeric. Matches the legacy
    ``_fmt`` helpers used to render Markdown reports.
    """
    number = as_float(value)
    if number is None:
        return none_text
    return f"{number:.{digits}f}"


def fmt_percent(value: object, *, digits: int = 2, none_text: str = "none") -> str:
    """Format a fractional value as a percentage.

    ``value`` is interpreted as a fraction (``0.5`` → ``"50.00%"``). Returns
    ``none_text`` when ``value`` is not numeric.
    """
    number = as_float(value)
    if number is None:
        return none_text
    return f"{number:.{digits}%}"


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(level: str | int | None = None) -> None:
    """Configure root logging once for the CLI.

    Resolution order for the level:

    1. The ``level`` argument, if provided.
    2. The ``NICHERADAR_LOG_LEVEL`` environment variable (e.g. ``"DEBUG"``).
    3. ``"INFO"``.

    Idempotent: if the root logger already has handlers (e.g. because pytest
    or another runner configured logging) the function is a no-op so we do
    not duplicate output.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    if level is None:
        level = os.environ.get("NICHERADAR_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = level.upper()

    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
