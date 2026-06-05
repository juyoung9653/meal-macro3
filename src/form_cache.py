"""GUI form state cache — per roster file.

Save/restore all GUI values when generating invoices/confirms.
Cache stored as JSON next to roster file (.form_cache.json).
Each roster file path maps to its own saved settings dict.
"""

import json
import os
from datetime import date

CACHE_FILENAME = ".form_cache.json"


# ── helpers ──


def _cache_path(roster_path: str) -> str:
    """Cache file lives beside roster file."""
    d = os.path.dirname(roster_path) if os.path.isabs(roster_path) else "."
    return os.path.join(d, CACHE_FILENAME)


def _roster_key(roster_path: str) -> str:
    """Unique key for a roster file (basename)."""
    return os.path.basename(roster_path)


def _date_to_str(d: date) -> str:
    return d.isoformat()


def _str_to_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ── public API ──


def _ns_key(roster_path: str, namespace: str) -> str:
    """Namespace-scoped key. Different tabs don't collide."""
    return f"{namespace}:{_roster_key(roster_path)}"


def save_form_cache(
    roster_path: str | None,
    data: dict,
    namespace: str = "default",
):
    """Save GUI state to per-file cache.

    ``data`` is a dict of all form values to persist.
    ``namespace`` keeps different tabs from colliding.
    Merges into existing cache for the same file.
    """
    if not roster_path:
        return
    key = _ns_key(roster_path, namespace)
    cpath = _cache_path(roster_path)
    try:
        if os.path.exists(cpath):
            with open(cpath, "r", encoding="utf-8") as f:
                store = json.load(f)
        else:
            store = {}
    except (json.JSONDecodeError, OSError):
        store = {}

    store[key] = data
    try:
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except OSError:
        pass  # best effort


def load_form_cache(
    roster_path: str | None,
    namespace: str = "default",
) -> dict | None:
    """Load GUI state for given roster file.

    Returns saved data dict, or None if no cache.
    """
    if not roster_path:
        return None
    key = _ns_key(roster_path, namespace)
    cpath = _cache_path(roster_path)
    try:
        if not os.path.exists(cpath):
            return None
        with open(cpath, "r", encoding="utf-8") as f:
            store = json.load(f)
        return store.get(key)
    except (json.JSONDecodeError, OSError):
        return None


def clear_form_cache(
    roster_path: str | None,
    namespace: str = "default",
):
    """Remove cache entry for given roster file."""
    if not roster_path:
        return
    key = _ns_key(roster_path, namespace)
    cpath = _cache_path(roster_path)
    try:
        if not os.path.exists(cpath):
            return
        with open(cpath, "r", encoding="utf-8") as f:
            store = json.load(f)
        store.pop(key, None)
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, OSError):
        pass
