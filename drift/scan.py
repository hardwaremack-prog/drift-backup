from __future__ import annotations
from pathlib import Path
from typing import Iterator

DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".cache", ".Trash", "$RECYCLE.BIN", "System Volume Information",
}
DEFAULT_EXCLUDE_SUFFIXES = {".tmp", ".DS_Store", ".swp"}


def walk_files(root: Path, extra_excludes: set[str] | None = None) -> Iterator[Path]:
    excludes = DEFAULT_EXCLUDE_DIRS | (extra_excludes or set())
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in excludes for part in path.parts):
            continue
        if path.suffix in DEFAULT_EXCLUDE_SUFFIXES or path.name in DEFAULT_EXCLUDE_SUFFIXES:
            continue
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue
        yield path
