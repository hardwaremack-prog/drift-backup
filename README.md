# Drift — backup that waits for the right moment

A real, working command-line backup tool. It deduplicates identical files,
compresses what's worth compressing, keeps a searchable history of every
file it's ever seen, and — the whole point — only runs while your computer
is actually resting.

## What it actually does

- **Deduplication** — every file is stored by a hash of its content. Back
  up the same photo from three folders, or the same PDF a dozen people
  emailed you, and it's stored on disk exactly once.
- **Smart compression** — text-like files get compressed (zlib, or lzma
  for bigger files where it wins); things that are already compressed
  (jpg, mp4, zip, docx, pdf...) are stored as-is instead of wasting CPU.
- **Incremental** — a file that hasn't changed size or modified-time since
  the last run is skipped entirely, not re-hashed and re-copied.
- **Adaptive, idle-aware scheduling** — `drift watch` checks real idle time
  (via OS-native APIs where available: Windows `GetLastInputInfo`, macOS
  `IOHIDSystem`, Linux `xprintidle`/DBus ScreenSaver — falling back to a
  CPU-load heuristic if none of those are available) and only starts a
  backup once you've genuinely stepped away. It re-checks between files,
  so it backs off immediately if you come back mid-run.
- **Instant, plain-language search** — `drift search "invoice"` searches
  every filename it's ever backed up, across every run, using SQLite
  full-text search.
- **One-command restore** — pull any file back out by path, optionally
  from a specific past snapshot.
- **Self-testing health check** — `drift drill` quietly restores a
  handful of random files to a scratch location and verifies they come
  back byte-for-byte, so "is my backup actually good?" has a real answer
  instead of a hopeful assumption.

## Install

```bash
python3 -m venv venv
source venv/bin/activate      # on Windows: venv\Scripts\activate
pip install -e .
```

Requires Python 3.10+. Installs `click`, `rich`, and `psutil`.

On Linux, real idle detection needs `xprintidle` (`sudo apt install
xprintidle`) or a desktop that answers the freedesktop ScreenSaver DBus
interface. Without either, Drift automatically falls back to a
CPU-load heuristic — it'll say so in `drift watch`'s startup message.

## Use it

```bash
# One-time setup: point it at a folder to protect, and where to store backups
drift init ~/Documents ~/DriftBackups

# Run one backup right now
drift backup ~/DriftBackups

# Let it run continuously, only backing up when you're away from the keyboard
drift watch ~/DriftBackups

# Find something
drift search ~/DriftBackups "invoice"

# Bring it back
drift restore ~/DriftBackups "Documents/invoice.xlsx" ~/Desktop/Restored

# Check on it
drift status ~/DriftBackups

# Prove it actually works
drift drill ~/DriftBackups
```

To have `drift watch` run automatically at login, wire it into your OS's
usual mechanism for that (a `launchd` agent on macOS, a systemd `--user`
service on Linux, or Task Scheduler on Windows) — that part's genuinely
OS-specific and worth doing properly rather than faked here.

## How the pieces fit together

```
drift/
  idle.py     — cross-platform "is this machine resting?" detection
  store.py    — content-addressable, self-describing compressed blob store
  index.py    — SQLite index: snapshots, files, full-text search, drill log
  scan.py     — walks a source folder, skipping junk (.git, node_modules, …)
  backup.py   — orchestrates one backup pass; the restore-drill self-test
  restore.py  — pulls a file back out of the store
  config.py   — per-destination settings (thresholds, sources)
  cli.py      — the `drift` command itself
```

## Honest limitations

- Deduplication is per whole file, not per block — copy a 10 GB video and
  change one byte, and it's stored as a new 10 GB file. Block-level dedup
  is a real project on top of this one, not a weekend addition.
- `drift watch` needs to be running (in a terminal, or wired into your
  OS's background-service mechanism) — it's not a menu-bar app with a
  toggle switch. That's a real GUI-and-installer project of its own.
- Idle detection genuinely varies by OS and desktop environment; the
  fallback heuristic is conservative but is still a heuristic.
