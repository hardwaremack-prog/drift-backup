"""
Content-addressable storage for file contents.

Every unique file (by content hash) is stored exactly once, compressed if
that's actually worth doing. This is what gives us deduplication for free:
back up the same photo from three folders, or the same PDF a hundred people
emailed you, and it only lives on disk once.
"""

from __future__ import annotations
import hashlib
import lzma
import zlib
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1024 * 1024  # 1 MB read chunks, keeps memory flat for huge files

# Extensions that are already compressed (photos, video, archives, modern
# office docs which are zip files under the hood). Compressing them again
# burns CPU for ~0 savings, so we store these raw.
_ALREADY_COMPRESSED = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp4", ".mov", ".mkv",
    ".mp3", ".m4a", ".flac", ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".docx", ".xlsx", ".pptx", ".pdf", ".png", ".webm", ".avi",
}


@dataclass
class StoredResult:
    hash: str
    original_size: int
    stored_size: int
    compression: str  # "none" | "zlib" | "lzma"
    already_existed: bool


class BlobStore:
    def __init__(self, root: Path):
        self.root = root
        self.objects_dir = root / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        sub = self.objects_dir / digest[:2]
        sub.mkdir(exist_ok=True)
        return sub / digest[2:]

    def exists(self, digest: str) -> bool:
        return self._path_for(digest).exists()

    def hash_file(self, path: Path) -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    # Every blob on disk is self-describing: the first byte records which
    # compression it uses. This matters specifically for deduplication —
    # when file #2 turns out to be identical to file #1, we must never
    # need to remember (in some separate table) how file #1 happened to be
    # compressed. The blob carries that fact with it, so a dedup hit can
    # never desync from how its bytes are actually stored.
    _HEADER = {"none": b"\x00", "zlib": b"\x01", "lzma": b"\x02"}
    _HEADER_R = {v: k for k, v in _HEADER.items()}

    def put(self, path: Path, precomputed_hash: str | None = None) -> StoredResult:
        digest, original_size = (
            (precomputed_hash, path.stat().st_size)
            if precomputed_hash
            else self.hash_file(path)
        )

        if self.exists(digest):
            stored_size = self._path_for(digest).stat().st_size
            existing_method = self._HEADER_R.get(
                self._path_for(digest).open("rb").read(1), "none"
            )
            return StoredResult(digest, original_size, stored_size, existing_method,
                                 already_existed=True)

        data = path.read_bytes()
        ext = path.suffix.lower()

        if ext in _ALREADY_COMPRESSED or original_size < 4096:
            payload, method = data, "none"
        else:
            # Try zlib first (fast); use lzma for a further squeeze on
            # larger, clearly-compressible text-like files.
            z = zlib.compress(data, level=6)
            if original_size > 2_000_000:
                x = lzma.compress(data, preset=4)
                payload, method = (x, "lzma") if len(x) < len(z) else (z, "zlib")
            else:
                payload, method = z, "zlib"
            if len(payload) >= original_size:
                payload, method = data, "none"

        target = self._path_for(digest)
        target.write_bytes(self._HEADER[method] + payload)
        return StoredResult(digest, original_size, len(payload) + 1, method, already_existed=False)

    def get(self, digest: str, method: str | None = None) -> bytes:
        raw = self._path_for(digest).read_bytes()
        header, body = raw[:1], raw[1:]
        actual_method = self._HEADER_R.get(header, "none")
        if actual_method == "zlib":
            return zlib.decompress(body)
        if actual_method == "lzma":
            return lzma.decompress(body)
        return body
