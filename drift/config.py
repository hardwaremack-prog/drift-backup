from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Config:
    sources: list[str] = field(default_factory=list)
    idle_threshold_seconds: int = 120
    cpu_ceiling_percent: int = 20
    check_interval_seconds: int = 20

    @classmethod
    def load(cls, backup_root: Path) -> "Config":
        p = backup_root / "config.json"
        if not p.exists():
            return cls()
        return cls(**json.loads(p.read_text()))

    def save(self, backup_root: Path):
        (backup_root / "config.json").write_text(json.dumps(asdict(self), indent=2))
