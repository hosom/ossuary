"""Ossuary CLI -- the deterministic surface.

Nothing here calls a model. Investigation happens through the MCP server
(`ossuary-mcp`), driven by whichever agent you are already talking to; these
commands are what you reach for around it: see what is on disk, read a session
outline by hand, render the report, inspect the taxonomy, export the findings.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import typer

from .adapters import ALL_SOURCES
from .pipeline import artifact_dir, read_manifest
from .report import write_report
from .store import SessionStore
from .taxonomy import TAXONOMY_FILENAME, Taxonomy

app = typer.Typer(
    name="ossuary",
    help=(
        "Find health issues in local LLM agent session transcripts. "
        "Investigation runs through the Ossuary plugin for Claude Code or "
        "Copilot CLI; these commands are the deterministic surface around it."
    ),
    no_args_is_help=True,
    add_completion=False,
)


def _echo(message: str = "") -> None:
    typer.echo(message)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command()
def sources(
    paths: list[Path] = typer.Argument(None, help="Optional explicit paths to search."),
    source: str | None = typer.Option(
        None, "--source", help=f"Limit to one source: {', '.join(ALL_SOURCES)}"
    ),
) -> None:
    """Show what Ossuary found on disk, with counts per source."""
    wanted = [source] if source else list(ALL_SOURCES)
    for name in wanted:
        if name not in ALL_SOURCES:
            _fail(f"unknown source {name!r}; expected one of {', '.join(ALL_SOURCES)}")

    from .adapters import get_adapter

    store = SessionStore()
    roots = [Path(p) for p in paths] if paths else None

    # One discovery pass over every requested source, so each file is claimed by
    # the adapter that recognises it. Discovering per-source in a loop would tell
    # each adapter it was the only one and count the same file several times.
    all_refs = store.discover(wanted, roots=roots)
    by_source: dict[str, list] = {name: [] for name in wanted}
    for ref in all_refs:
        by_source.setdefault(ref.source, []).append(ref)

    total = 0
    for name in wanted:
        refs = by_source.get(name, [])
        adapter_roots = roots if roots else get_adapter(name).default_roots()
        _echo(f"{name}: {len(refs)} session(s)")
        for root in adapter_roots:
            marker = "" if Path(root).expanduser().exists() else "  (not present)"
            _echo(f"    searched: {root}{marker}")
        if refs:
            newest = max(refs, key=lambda r: r.mtime or datetime.min)
            size = sum(r.size_bytes for r in refs)
            _echo(f"    total size: {size / 1_048_576:.1f} MiB")
            _echo(
                f"    most recent: {newest.session_id} "
                f"({newest.mtime.strftime('%Y-%m-%d %H:%M') if newest.mtime else 'unknown'})"
            )
        total += len(refs)
        _echo()

    _echo(f"{total} session(s) total.")


@app.command()
def report(
    out: Path = typer.Option(Path("report.html"), "--out", help="Output HTML file."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the report in a browser when done."
    ),
) -> None:
    """Render the HTML report from artifacts an investigation wrote. Runs no inference."""
    base = Path.cwd()
    try:
        manifest = read_manifest(base)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return

    # Re-parse the transcripts to pull evidence excerpts. Deterministic and
    # cheap; no model is involved.
    store = SessionStore()
    missing = 0
    for scan_result in manifest.scans:
        path = Path(scan_result.path)
        if not path.exists():
            missing += 1
            continue
        try:
            from .adapters import get_adapter
            from .models import SessionRef

            ref = SessionRef(
                session_id=scan_result.session_id,
                source=scan_result.source,
                path=str(path),
                size_bytes=path.stat().st_size,
            )
            store.add(get_adapter(scan_result.source).parse(ref))
        except Exception:  # noqa: BLE001 - evidence is best-effort
            missing += 1

    if missing:
        typer.secho(
            f"warning: {missing} session file(s) unavailable; their evidence "
            f"excerpts are omitted from the report.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    path = write_report(manifest, out, store=store, open_browser=open_browser)
    _echo(f"Wrote {path.resolve()}")


@app.command()
def taxonomy(
    show: bool = typer.Option(True, "--show/--clear", help="Show or clear the stored taxonomy."),
) -> None:
    """Inspect or reset the persisted cluster taxonomy."""
    path = artifact_dir(Path.cwd()) / TAXONOMY_FILENAME
    store = Taxonomy(path)

    if not show:
        if path.exists():
            path.unlink()
            _echo(f"Cleared {path}. The next run will treat every cluster as new.")
        else:
            _echo("No stored taxonomy to clear.")
        return

    known = store.known()
    if not known:
        _echo(f"No stored taxonomy at {path}.")
        return
    _echo(f"{len(known)} known cluster(s) in {path}:\n")
    for entry in sorted(known, key=lambda e: e.get("name", "")):
        _echo(f"  {entry['cluster_id']}")
        _echo(f"    name: {entry.get('name')}")
        _echo(f"    first seen: {entry.get('first_seen_run')}  last: {entry.get('last_seen_run')}")
        _echo(f"    runs seen: {entry.get('total_runs_seen')}")


@app.command()
def outline(
    session_id: str = typer.Argument(..., help="Session id, or a path to a transcript."),
    source: str | None = typer.Option(None, "--source", help="Limit discovery to one source."),
) -> None:
    """Print the outline for one session. Deterministic; no model is involved."""
    store = SessionStore()
    path = Path(session_id)
    if path.exists():
        refs = store.discover([source] if source else list(ALL_SOURCES), roots=[path])
    else:
        refs = [
            r
            for r in store.discover([source] if source else list(ALL_SOURCES))
            if r.session_id == session_id or r.session_id.startswith(session_id)
        ]

    if not refs:
        _fail(f"no session matching {session_id!r}")
        return

    session = store.load(refs[0])
    _echo(store.outline(session.session_id))


@app.command("export")
def export_issues(
    out: Path = typer.Option(Path("issues.jsonl"), "--out", help="Output JSONL file."),
) -> None:
    """Export every issue from the last run as JSONL."""
    try:
        manifest = read_manifest(Path.cwd())
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return

    cluster_of = {
        issue_id: cluster.cluster_id
        for cluster in manifest.clusters
        for issue_id in cluster.member_issue_ids
    }
    count = 0
    with out.open("w", encoding="utf-8") as handle:
        for scan_result in manifest.scans:
            for issue in scan_result.issues:
                row = issue.model_dump(mode="json")
                row["cluster_id"] = cluster_of.get(issue.issue_id)
                row["run_id"] = manifest.run_id
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
    _echo(f"Wrote {count} issue(s) to {out}")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
