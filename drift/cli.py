from __future__ import annotations
import time
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .backup import run_backup, run_restore_drill
from .config import Config
from .idle import IdleMonitor
from .index import Index
from .restore import restore_file

console = Console()


def _humanize(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _when(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hr ago"
    return f"{int(delta // 86400)} days ago"


@click.group()
def main():
    """Drift — backup that waits for the right moment."""


@main.command()
@click.argument("source", type=click.Path(exists=True, file_okay=False))
@click.argument("destination", type=click.Path(file_okay=False))
def init(source, destination):
    """Set up a new backup: SOURCE folder -> DESTINATION backup store."""
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    cfg = Config(sources=[str(Path(source).resolve())])
    cfg.save(dest)
    Index(dest / "drift.db").close()
    console.print(Panel.fit(
        f"[bold]Drift is set up.[/bold]\n\n"
        f"Watching: [cyan]{Path(source).resolve()}[/cyan]\n"
        f"Storing backups in: [cyan]{dest.resolve()}[/cyan]\n\n"
        f"Run [bold]drift backup {destination}[/bold] for a one-off backup, or\n"
        f"[bold]drift watch {destination}[/bold] to let it run adaptively in the background.",
        title="drift init", border_style="yellow",
    ))


@main.command()
@click.argument("destination", type=click.Path(exists=True, file_okay=False))
@click.option("--force", is_flag=True, help="Back up now regardless of idle/CPU state.")
def backup(destination, force):
    """Run one backup pass right now."""
    dest = Path(destination)
    cfg = Config.load(dest)
    if not cfg.sources:
        console.print("[red]No source configured — run `drift init SOURCE DESTINATION` first.[/red]")
        return
    for src in cfg.sources:
        console.print(f"[dim]Backing up[/dim] [cyan]{src}[/cyan] [dim]→[/dim] [cyan]{dest}[/cyan]")
        result = run_backup(Path(src), dest, force=force, respect_idle=not force, config=cfg)
        if not result["ran"]:
            console.print(f"[yellow]Skipped — {result['reason']}[/yellow] "
                           f"[dim]({result['detail']})[/dim]  (use --force to override)")
            continue
        saved = result["bytes_original"] - result["bytes_stored"]
        console.print(Panel.fit(
            f"Scanned [bold]{result['files_scanned']}[/bold] files, "
            f"[bold]{result['files_changed']}[/bold] changed.\n"
            f"Deduplicated [bold]{result['dedup_hits']}[/bold] files "
            f"(saved [bold]{_humanize(saved)}[/bold] vs. storing everything raw).\n"
            + ("[yellow]Paused early — you came back to the keyboard.[/yellow]"
               if result["paused_early"] else "[green]Completed.[/green]"),
            title="backup complete", border_style="green",
        ))


@main.command()
@click.argument("destination", type=click.Path(exists=True, file_okay=False))
def watch(destination):
    """
    Run forever, backing up only when the machine looks genuinely at rest.
    Ctrl+C to stop.
    """
    dest = Path(destination)
    cfg = Config.load(dest)
    if not cfg.sources:
        console.print("[red]No source configured — run `drift init SOURCE DESTINATION` first.[/red]")
        return
    idle = IdleMonitor()
    console.print(Panel.fit(
        f"Watching for rest (idle detection: [bold]{idle.method}[/bold]).\n"
        f"Threshold: idle for {cfg.idle_threshold_seconds}s, checking every "
        f"{cfg.check_interval_seconds}s. Ctrl+C to stop.",
        title="drift watch", border_style="yellow",
    ))
    try:
        while True:
            snap = idle.snapshot()
            ts = datetime.now().strftime("%H:%M:%S")
            if snap["idle_seconds"] >= cfg.idle_threshold_seconds and snap["battery_ok"]:
                console.print(f"[dim]{ts}[/dim] [green]at rest[/green] "
                              f"(idle {snap['idle_seconds']:.0f}s) — backing up...")
                for src in cfg.sources:
                    run_backup(Path(src), dest, force=False, respect_idle=True,
                               idle=idle, config=cfg)
            else:
                console.print(f"[dim]{ts}[/dim] [yellow]active[/yellow] "
                               f"(idle {snap['idle_seconds']:.0f}s, cpu {snap['cpu_percent']:.0f}%) "
                               f"— standing by")
            time.sleep(cfg.check_interval_seconds)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


@main.command()
@click.argument("destination", type=click.Path(exists=True, file_okay=False))
@click.argument("term")
def search(destination, term):
    """Search everything Drift has ever backed up."""
    index = Index(Path(destination) / "drift.db")
    rows = index.search(term)
    index.close()
    if not rows:
        console.print(f"[yellow]Nothing found for[/yellow] \"{term}\"")
        return
    table = Table(title=f'Results for "{term}"', border_style="dim")
    table.add_column("File")
    table.add_column("Size", justify="right")
    table.add_column("Backed up")
    table.add_column("Snapshot", justify="right")
    for r in rows:
        table.add_row(r["relpath"], _humanize(r["size"]), _when(r["mtime"]), str(r["snapshot_id"]))
    console.print(table)
    console.print("[dim]Restore with:[/dim] drift restore DESTINATION \"<file path shown above>\" TO_FOLDER")


@main.command()
@click.argument("destination", type=click.Path(exists=True, file_okay=False))
@click.argument("relpath")
@click.argument("to_folder", type=click.Path(file_okay=False))
@click.option("--snapshot", type=int, default=None, help="Restore a specific snapshot id instead of latest.")
def restore(destination, relpath, to_folder, snapshot):
    """Restore RELPATH (as shown by `drift search`) into TO_FOLDER."""
    try:
        out = restore_file(Path(destination), relpath, Path(to_folder), snapshot_id=snapshot)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return
    console.print(Panel.fit(f"Restored to [cyan]{out}[/cyan]", title="restore complete", border_style="green"))


@main.command()
@click.argument("destination", type=click.Path(exists=True, file_okay=False))
def status(destination):
    """Show the health dashboard: what's protected, dedup savings, self-test results."""
    dest = Path(destination)
    index = Index(dest / "drift.db")
    s = index.stats()
    index.close()

    saved = (s["bytes_original"] or 0) - (s["bytes_stored"] or 0)
    drill_rate = (s["drills_passed"] / s["drills_total"] * 100) if s["drills_total"] else None

    lines = [
        f"Files protected: [bold]{s['file_count']}[/bold]",
        f"Backup runs so far: [bold]{s['snapshot_count']}[/bold]  ·  last: {_when(s['last_backup'])}",
        f"Original size: {_humanize(s['bytes_original'] or 0)}  →  stored: "
        f"{_humanize(s['bytes_stored'] or 0)}  [green](saved {_humanize(saved)})[/green]",
        f"Duplicate files skipped: [bold]{s['dedup_hits']}[/bold]",
    ]
    if drill_rate is not None:
        color = "green" if drill_rate == 100 else "yellow"
        lines.append(f"Self-test restores: [{color}]{s['drills_passed']}/{s['drills_total']} passed[/{color}] "
                      f"· last check {_when(s['last_drill'])}")
    else:
        lines.append("Self-test restores: [dim]none run yet — try `drift drill`[/dim]")

    console.print(Panel.fit("\n".join(lines), title="drift status", border_style="cyan"))


@main.command()
@click.argument("destination", type=click.Path(exists=True, file_okay=False))
@click.option("-n", "--count", default=3, help="How many random files to test-restore.")
def drill(destination, count):
    """Quietly test-restore a few random files and verify they're intact."""
    console.print("[dim]Running restore drill...[/dim]")
    results = run_restore_drill(Path(destination), n=count)
    if not results:
        console.print("[yellow]Nothing to test yet — run a backup first.[/yellow]")
        return
    table = Table(border_style="dim")
    table.add_column("File")
    table.add_column("Result")
    for relpath, ok, detail in results:
        mark = "[green]✓ verified[/green]" if ok else f"[red]✗ {detail}[/red]"
        table.add_row(relpath, mark)
    console.print(table)


if __name__ == "__main__":
    main()
