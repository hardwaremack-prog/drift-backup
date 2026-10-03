from __future__ import annotations
from pathlib import Path

from .index import Index
from .store import BlobStore


def restore_file(backup_root: Path, relpath: str, dest_dir: Path,
                  snapshot_id: int | None = None) -> Path:
    index = Index(backup_root / "drift.db")
    store = BlobStore(backup_root)
    try:
        if snapshot_id is not None:
            row = index.conn.execute(
                "SELECT * FROM files WHERE relpath = ? AND snapshot_id = ?",
                (relpath, snapshot_id),
            ).fetchone()
        else:
            row = index.latest_version(relpath)
        if row is None:
            raise FileNotFoundError(f"No backed-up version found for {relpath}")

        data = store.get(row["hash"], row["compression"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / Path(relpath).name
        # avoid clobbering an existing file at the restore destination
        n = 1
        base_out = out_path
        while out_path.exists():
            out_path = base_out.with_name(f"{base_out.stem} (restored {n}){base_out.suffix}")
            n += 1
        out_path.write_bytes(data)
        return out_path
    finally:
        index.close()
