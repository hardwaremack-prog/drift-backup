from __future__ import annotations
import time
from pathlib import Path

from rich.console import Console
from rich.progress import (
    Progress, BarColumn, TextColumn, TimeRemainingColumn, SpinnerColumn,
)

from .config import Config
from .idle import IdleMonitor
from .index import Index
from .scan import walk_files
from .store import BlobStore

console = Console()


class PausedByActivity(Exception):
    """Raised to unwind a backup pass early because the user came back."""


def run_backup(
    source: Path,
    backup_root: Path,
    *,
    force: bool = False,
    respect_idle: bool = True,
    idle: IdleMonitor | None = None,
    config: Config | None = None,
) -> dict:
    """
    Runs one incremental backup pass of `source` into `backup_root`.

    If respect_idle is True and the machine isn't currently at rest, this
    backs off immediately rather than starting (used by `drift watch`).
    Once running, it also re-checks idle state between files so a backup
    already in progress steps aside the moment you start typing again.
    """
    backup_root.mkdir(parents=True, exist_ok=True)
    store = BlobStore(backup_root)
    index = Index(backup_root / "drift.db")
    idle = idle or IdleMonitor()
    config = config or Config.load(backup_root)

    if respect_idle and not force:
        snap = idle.snapshot()
        if snap["idle_seconds"] < config.idle_threshold_seconds or not snap["battery_ok"]:
            index.close()
            return {"ran": False, "reason": "system not at rest", "detail": snap}

    files = list(walk_files(source))
    snapshot_id = index.start_snapshot(str(source))

    scanned = changed = dedup_hits = 0
    bytes_original = bytes_stored = 0
    paused = False

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Backing up", total=len(files))

        for i, path in enumerate(files):
            # Check every ~15 files whether the user has come back.
            if respect_idle and not force and i % 15 == 0 and i > 0:
                snap = idle.snapshot()
                if snap["idle_seconds"] < config.idle_threshold_seconds:
                    paused = True
                    break

            scanned += 1
            relpath = str(path.relative_to(source))
            try:
                st = path.stat()
            except OSError:
                progress.advance(task)
                continue

            known_hash = index.unchanged(relpath, st.st_size, st.st_mtime)
            if known_hash is not None:
                progress.advance(task)
                continue

            result = store.put(path)
            index.record_file(snapshot_id, relpath, st.st_size, st.st_mtime,
                               result.hash, result.compression)
            changed += 1
            bytes_original += result.original_size
            bytes_stored += 0 if result.already_existed else result.stored_size
            if result.already_existed:
                dedup_hits += 1

            progress.advance(task)

    index.finish_snapshot(
        snapshot_id,
        files_scanned=scanned,
        files_changed=changed,
        bytes_original=bytes_original,
        bytes_stored=bytes_stored,
        dedup_hits=dedup_hits,
    )
    index.close()

    return {
        "ran": True,
        "paused_early": paused,
        "files_scanned": scanned,
        "files_changed": changed,
        "dedup_hits": dedup_hits,
        "bytes_original": bytes_original,
        "bytes_stored": bytes_stored,
    }


def run_restore_drill(backup_root: Path, n: int = 3) -> list[tuple[str, bool, str]]:
    """
    Quietly restores a few random files to a throwaway temp folder and
    confirms they come back byte-for-byte. This is what lets Drift claim a
    real, tested health score instead of just "the copy job finished".
    """
    import tempfile
    import hashlib

    index = Index(backup_root / "drift.db")
    store = BlobStore(backup_root)
    results = []
    for relpath in index.sample_paths_for_drill(n):
        row = index.latest_version(relpath)
        if row is None:
            continue
        try:
            data = store.get(row["hash"], row["compression"])
            actual_hash = hashlib.sha256(data).hexdigest()
            ok = actual_hash == row["hash"]
            with tempfile.NamedTemporaryFile(delete=True) as tmp:
                tmp.write(data)
            detail = "restored and verified" if ok else "hash mismatch on restore!"
        except Exception as e:
            ok = False
            detail = f"failed: {e}"
        index.record_drill(relpath, ok, detail)
        results.append((relpath, ok, detail))
    index.close()
    return results
