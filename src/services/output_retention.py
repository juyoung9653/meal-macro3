"""Remove expired regular files only inside the application's output directory."""
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import stat


@dataclass(frozen=True)
class CleanupResult:
    deleted: tuple[Path, ...] = ()
    failed: tuple[Path, ...] = ()


def retention_cutoff(now: datetime, months: int) -> datetime:
    year, month = divmod(now.year * 12 + now.month - 1 - months, 12)
    month += 1
    return now.replace(year=year, month=month, day=min(now.day, monthrange(year, month)[1]))


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def cleanup_output_files(base_directory: Path, months: int, *, now: datetime | None = None) -> CleanupResult:
    if type(months) is not int or not 0 <= months <= 120:
        raise ValueError("보관기간은 0~120개월이어야 합니다.")
    if months == 0:
        return CleanupResult()
    base = Path(base_directory).resolve()
    root = base / "output"
    if not root.exists():
        return CleanupResult()
    if _linked(root) or root.resolve() != root or not root.is_dir():
        return CleanupResult()
    cutoff = retention_cutoff(now or datetime.now().astimezone(), months).timestamp()
    deleted, failed = [], []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            if _linked(directory) or not directory.resolve().is_relative_to(root):
                continue
            children = tuple(directory.iterdir())
        except OSError:
            failed.append(directory)
            continue
        for path in children:
            try:
                if _linked(path) or not path.resolve().is_relative_to(root):
                    continue
                info = path.stat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_mtime <= cutoff:
                    # Recheck both containment and timestamp just before removal.
                    if _linked(path) or not path.resolve().is_relative_to(root):
                        continue
                    if path.stat().st_mtime <= cutoff:
                        path.unlink()
                        deleted.append(path)
            except OSError:
                failed.append(path)
    if deleted or failed:
        logging.getLogger(__name__).info("Output retention: %d removed, %d unavailable", len(deleted), len(failed))
    return CleanupResult(tuple(deleted), tuple(failed))
