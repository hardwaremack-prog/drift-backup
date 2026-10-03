"""
The searchable index of everything Drift has ever backed up.

One row per (snapshot, file) so you can find "that invoice" across every
backup run ever taken, and jump straight to restoring the version you want.
"""

from __future__ import annotations
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    source_root TEXT NOT NULL,
    files_scanned INTEGER DEFAULT 0,
    files_changed INTEGER DEFAULT 0,
    bytes_original INTEGER DEFAULT 0,
    bytes_stored INTEGER DEFAULT 0,
    dedup_hits INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    hash TEXT NOT NULL,
    compression TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_files_relpath ON files(relpath);
CREATE INDEX IF NOT EXISTS idx_files_snapshot ON files(snapshot_id);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    relpath, content='files', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, relpath) VALUES (new.id, new.relpath);
END;

-- last known state per relpath, so incremental backups can skip
-- unchanged files without re-hashing every time.
CREATE TABLE IF NOT EXISTS last_seen (
    relpath TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    hash TEXT NOT NULL
);

-- weekly self-test results ("restore drill")
CREATE TABLE IF NOT EXISTS restore_drills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at REAL NOT NULL,
    relpath TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT
);
"""


class Index:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def start_snapshot(self, source_root: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO snapshots (started_at, source_root) VALUES (?, ?)",
            (time.time(), source_root),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_snapshot(self, snapshot_id: int, **stats):
        stats["finished_at"] = time.time()
        cols = ", ".join(f"{k} = :{k}" for k in stats)
        stats["id"] = snapshot_id
        self.conn.execute(f"UPDATE snapshots SET {cols} WHERE id = :id", stats)
        self.conn.commit()

    def record_file(self, snapshot_id: int, relpath: str, size: int, mtime: float,
                     digest: str, compression: str):
        self.conn.execute(
            "INSERT INTO files (snapshot_id, relpath, size, mtime, hash, compression) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot_id, relpath, size, mtime, digest, compression),
        )
        self.conn.execute(
            "INSERT INTO last_seen (relpath, size, mtime, hash) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(relpath) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, hash=excluded.hash",
            (relpath, size, mtime, digest),
        )
        self.conn.commit()

    def unchanged(self, relpath: str, size: int, mtime: float) -> str | None:
        """Returns the known hash if this file looks unchanged, else None."""
        row = self.conn.execute(
            "SELECT size, mtime, hash FROM last_seen WHERE relpath = ?", (relpath,)
        ).fetchone()
        if row and row["size"] == size and abs(row["mtime"] - mtime) < 1e-6:
            return row["hash"]
        return None

    def search(self, term: str, limit: int = 25) -> list[sqlite3.Row]:
        term = term.strip()
        if not term:
            return []
        # trigram FTS wants no special query syntax troubles; escape quotes
        safe = term.replace('"', '""')
        try:
            rows = self.conn.execute(
                """
                SELECT f.relpath, f.size, f.mtime, f.hash, f.compression,
                       s.id as snapshot_id, s.started_at
                FROM files_fts
                JOIN files f ON f.id = files_fts.rowid
                JOIN snapshots s ON s.id = f.snapshot_id
                WHERE files_fts MATCH ?
                ORDER BY f.mtime DESC
                LIMIT ?
                """,
                (f'"{safe}"', limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            return rows
        # fallback plain LIKE search (handles short/odd queries FTS trigram might miss)
        return self.conn.execute(
            """
            SELECT f.relpath, f.size, f.mtime, f.hash, f.compression,
                   s.id as snapshot_id, s.started_at
            FROM files f JOIN snapshots s ON s.id = f.snapshot_id
            WHERE f.relpath LIKE ?
            ORDER BY f.mtime DESC LIMIT ?
            """,
            (f"%{term}%", limit),
        ).fetchall()

    def latest_version(self, relpath: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT f.*, s.started_at FROM files f JOIN snapshots s ON s.id = f.snapshot_id "
            "WHERE f.relpath = ? ORDER BY f.mtime DESC LIMIT 1",
            (relpath,),
        ).fetchone()

    def history(self, relpath: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT f.*, s.started_at FROM files f JOIN snapshots s ON s.id = f.snapshot_id "
            "WHERE f.relpath = ? ORDER BY f.mtime DESC",
            (relpath,),
        ).fetchall()

    def stats(self) -> dict:
        snap = self.conn.execute(
            "SELECT COUNT(*) c, MAX(finished_at) last FROM snapshots WHERE finished_at IS NOT NULL"
        ).fetchone()
        totals = self.conn.execute(
            "SELECT SUM(bytes_original) o, SUM(bytes_stored) s, SUM(dedup_hits) d FROM snapshots"
        ).fetchone()
        file_count = self.conn.execute("SELECT COUNT(DISTINCT relpath) c FROM last_seen").fetchone()
        drills = self.conn.execute(
            "SELECT COUNT(*) total, SUM(ok) passed, MAX(ran_at) last FROM restore_drills"
        ).fetchone()
        return {
            "snapshot_count": snap["c"],
            "last_backup": snap["last"],
            "bytes_original": totals["o"] or 0,
            "bytes_stored": totals["s"] or 0,
            "dedup_hits": totals["d"] or 0,
            "file_count": file_count["c"] or 0,
            "drills_total": drills["total"] or 0,
            "drills_passed": drills["passed"] or 0,
            "last_drill": drills["last"],
        }

    def record_drill(self, relpath: str, ok: bool, detail: str = ""):
        self.conn.execute(
            "INSERT INTO restore_drills (ran_at, relpath, ok, detail) VALUES (?, ?, ?, ?)",
            (time.time(), relpath, int(ok), detail),
        )
        self.conn.commit()

    def sample_paths_for_drill(self, n: int = 3) -> list[str]:
        rows = self.conn.execute(
            "SELECT relpath FROM last_seen ORDER BY RANDOM() LIMIT ?", (n,)
        ).fetchall()
        return [r["relpath"] for r in rows]

    def close(self):
        self.conn.close()
