"""GUI form state cache — per roster file.

Save/restore all GUI values when generating invoices/confirms.
Cache stored as JSON in configurable directory under project root.
Each roster file path maps to its own saved JSON file.
Auto-evicts oldest files when total cache exceeds configured limit.
"""

import hashlib
import json
import os
from datetime import date

from .settings import get_settings

# ── helpers ──


def _cache_dir_name() -> str:
    return get_settings().get("cache", "dir_name", default="cahce")


def _max_cache_bytes() -> int:
    mb = get_settings().get("cache", "max_size_mb", default=100)
    return mb * 1024 * 1024


def _project_root() -> str:
    """Project root = grandparent of src/."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cache_dir() -> str:
    """Absolute path to the cahce/ directory (created if missing)."""
    d = os.path.join(_project_root(), _cache_dir_name())
    os.makedirs(d, exist_ok=True)
    return d


def _sanitized_name(roster_path: str) -> str:
    """Unique safe filename for a roster path."""
    abs_path = (
        os.path.abspath(roster_path) if not os.path.isabs(roster_path) else roster_path
    )
    base = os.path.basename(abs_path)
    h = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:12]
    root, _ = os.path.splitext(base)
    return f".form_cache.{root}.{h}.json"


def _cache_path(roster_path: str) -> str:
    return os.path.join(_cache_dir(), _sanitized_name(roster_path))


def _roster_key(roster_path: str) -> str:
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
    """Remove oldest cache files until total size ≤ limit."""
    cache_dir = _cache_dir()
    max_bytes = _max_cache_bytes()
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

        if total <= max_bytes:
            return

        entries.sort(key=lambda x: x[0])

        for mtime, size, fpath in entries:
            if total <= max_bytes:
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
    return f"{namespace}:{_roster_key(roster_path)}"


def save_form_cache(roster_path: str | None, data: dict, namespace: str = "default"):
    """Save GUI state to per-file cache. Auto-evicts if over limit."""
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
        pass

    _evict_if_over_limit()


def load_form_cache(roster_path: str | None, namespace: str = "default") -> dict | None:
    """Load GUI state for given roster file. Returns None if no cache."""
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


def clear_form_cache(roster_path: str | None, namespace: str = "default"):
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
