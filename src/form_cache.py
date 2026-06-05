"""GUI form state cache — per roster file.

Save/restore all GUI values when generating invoices/confirms.
Cache stored as JSON in ``cahce/`` directory under project root.
Each roster file path maps to its own saved JSON file.
Auto-evicts oldest files when total cache exceeds 100 MB.
"""

import hashlib
import json
import os
from datetime import date

CACHE_DIR_NAME = "cahce"
MAX_CACHE_BYTES = 100 * 1024 * 1024  # 100 MB


# ── helpers ──


def _project_root() -> str:
    """Project root = grandparent of src/."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cache_dir() -> str:
    """Absolute path to the cahce/ directory (created if missing)."""
    d = os.path.join(_project_root(), CACHE_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _sanitized_name(roster_path: str) -> str:
    """Unique safe filename for a roster path.

    Uses a short hash so the name stays readable but collision-free.
    """
    abs_path = (
        os.path.abspath(roster_path) if not os.path.isabs(roster_path) else roster_path
    )
    base = os.path.basename(abs_path)
    h = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:12]
    # strip .xlsx / .xls extension for readability
    root, _ = os.path.splitext(base)
    return f".form_cache.{root}.{h}.json"


def _cache_path(roster_path: str) -> str:
    """Cache file path inside cahce/ directory."""
    return os.path.join(_cache_dir(), _sanitized_name(roster_path))


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


# ── cache size management ──


def _evict_if_over_limit():
    """Remove oldest cache files until total size ≤ MAX_CACHE_BYTES."""
    cache_dir = _cache_dir()
    try:
        entries = []
        total = 0
        for fname in os.listdir(cache_dir):
            fpath = os.path.join(cache_dir, fname)
            if not fname.startswith(".form_cache.") or not fname.endswith(".json"):
                continue
            try:
                st = os.stat(fpath)
                entries.append((st.st_mtime, st.st_size, fpath))
                total += st.st_size
            except OSError:
                continue

        if total <= MAX_CACHE_BYTES:
            return

        # oldest first
        entries.sort(key=lambda x: x[0])

        for mtime, size, fpath in entries:
            if total <= MAX_CACHE_BYTES:
                break
            try:
                os.remove(fpath)
                total -= size
            except OSError:
                continue
    except OSError:
        pass


# ── public API ──


def _ns_key(roster_path: str, namespace: str) -> str:
    """Namespace-scoped key. Different tabs don't collide."""
    return f"{namespace}:{_roster_key(roster_path)}"


def save_form_cache(
    roster_path: str | None,
    data: dict,
    namespace: str = "default",
):
    """Save GUI state to per-file cache inside ``cahce/``.

    ``data`` is a dict of all form values to persist.
    ``namespace`` keeps different tabs from colliding.
    Merges into existing cache for the same file.
    Auto-evicts oldest cache files if total exceeds 100 MB.
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

    _evict_if_over_limit()


def load_form_cache(
    roster_path: str | None,
    namespace: str = "default",
) -> dict | None:
    """Load GUI state for given roster file from ``cahce/``.

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
    """Remove cache entry for given roster file from ``cahce/``."""
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
