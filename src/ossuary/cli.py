"""Ossuary CLI.

`scan` is expensive and writes artifacts to `.ossuary/`. `report` is cheap, reads
those artifacts, and renders HTML. They are deliberately separate commands: the
report design gets iterated on dozens of times and must never re-pay for
inference to do it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from .adapters import ALL_SOURCES
from .aggregate import compute_tool_stats, corpus_event_count, render_tool_stats
from .cache import Cache
from .config import find_config, load_config
from .models import RunManifest
from .pipeline import (
    artifact_dir,
    cluster_issues,
    corpus_summary,
    make_run_id,
    read_manifest,
    scan_session,
    write_manifest,
)
from .redact import Redactor
from .report import write_report
from .store import SessionStore
from .taxonomy import TAXONOMY_FILENAME, Taxonomy

app = typer.Typer(
    name="ossuary",
    help="Find health issues in local LLM agent session transcripts.",
    no_args_is_help=True,
    add_completion=False,
)
agents_app = typer.Typer(help="Inspect and test the configured agents.", no_args_is_help=True)
app.add_typer(agents_app, name="agents")


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
def scan(
    paths: list[Path] = typer.Argument(None, help="Optional explicit paths to scan."),
    source: str | None = typer.Option(
        None, "--source", help=f"Limit to one source: {', '.join(ALL_SOURCES)}"
    ),
    model: str | None = typer.Option(
        None, "--model", help="Override the scanner model from agents.yaml."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Scan at most N sessions."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore and overwrite the cache."),
    no_redact: bool = typer.Option(
        False,
        "--no-redact",
        help="Disable redaction. Transcripts will be sent to the model verbatim.",
    ),
    no_cluster: bool = typer.Option(
        False, "--no-cluster", help="Skip Agent B; scan sessions only."
    ),
    config_path: Path | None = typer.Option(None, "--config", help="Path to agents.yaml."),
) -> None:
    """Scan sessions, find issues, cluster them, and write artifacts to .ossuary/."""
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return

    if model:
        config.agents["scanner"] = config.scanner.model_copy(update={"model": model})

    wanted = [source] if source else list(ALL_SOURCES)
    for name in wanted:
        if name not in ALL_SOURCES:
            _fail(f"unknown source {name!r}; expected one of {', '.join(ALL_SOURCES)}")

    if no_redact:
        typer.secho(
            "warning: redaction disabled; transcript content will be sent to the "
            "model verbatim, including any credentials it contains.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    redactor = Redactor(enabled=not no_redact)
    roots = [Path(p) for p in paths] if paths else None
    store = SessionStore(redactor=redactor, roots=roots)

    refs = store.discover(wanted, roots=roots)
    if not refs:
        _fail("no sessions found. Run `ossuary sources` to see where Ossuary looked.")
        return

    refs.sort(key=lambda r: r.mtime or datetime.min, reverse=True)
    if limit:
        refs = refs[:limit]

    base = Path.cwd()
    cache = Cache(artifact_dir(base), enabled=not no_cache)
    run_id = make_run_id()
    started = datetime.now(timezone.utc)

    _echo(f"Parsing {len(refs)} session(s)...")
    sessions = []
    errors: list[str] = []
    for ref in refs:
        try:
            sessions.append(store.load(ref))
        except Exception as exc:  # noqa: BLE001 - one bad file must not end the run
            errors.append(f"failed to parse {ref.path}: {type(exc).__name__}: {exc}")

    if not sessions:
        _fail("no sessions could be parsed.")
        return

    degraded = sum(s.parse_error_count for s in sessions)
    _echo(
        f"Parsed {len(sessions)} session(s), {corpus_event_count(sessions):,} events"
        + (f", {degraded} degraded line(s)." if degraded else ".")
    )

    # Computed before scanning so Agent A's `tool_stats` tool can answer
    # corpus-wide questions from turn one.
    tool_stats = compute_tool_stats(sessions)

    _echo(f"Scanning with {config.scanner.model} (max {config.scanner.max_turns} turns/session)...")
    scans = []
    with typer.progressbar(sessions, label="  sessions") as progress:
        for session in progress:
            scan_result = scan_session(
                session,
                store=store,
                config=config,
                cache=cache,
                tool_stats=tool_stats,
                redacted=not no_redact,
            )
            scans.append(scan_result)
            if scan_result.error:
                errors.append(f"{session.session_id}: {scan_result.error}")

    issues = [issue for scan_result in scans for issue in scan_result.issues]
    cached_count = sum(1 for s in scans if s.from_cache)
    _echo(
        f"Found {len(issues)} issue(s) across {len(scans)} session(s) "
        f"({cached_count} served from cache)."
    )

    taxonomy = Taxonomy(artifact_dir(base) / TAXONOMY_FILENAME)
    clusters = []
    if issues and not no_cluster:
        _echo(f"Clustering with {config.clusterer.model}...")
        clusters, cluster_error = cluster_issues(
            issues, tool_stats, config=config, taxonomy=taxonomy, run_id=run_id
        )
        if cluster_error:
            errors.append(cluster_error)
            typer.secho(f"warning: {cluster_error}", fg=typer.colors.YELLOW, err=True)
        taxonomy.update(clusters, run_id=run_id)
        taxonomy.save()
        new_count = sum(1 for c in clusters if c.is_new_this_run)
        _echo(f"Produced {len(clusters)} cluster(s), {new_count} new this run.")
    elif no_cluster:
        _echo("Skipping clustering (--no-cluster).")

    manifest = RunManifest(
        run_id=run_id,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        scanner_model=config.scanner.model,
        clusterer_model=config.clusterer.model,
        prompt_version=config.scanner.prompt_version,
        redaction_enabled=not no_redact,
        session_count=len(sessions),
        event_count=corpus_event_count(sessions),
        issue_count=len(issues),
        cached_session_count=cached_count,
        sources=corpus_summary(sessions),
        scans=scans,
        tool_stats=tool_stats,
        clusters=clusters,
        errors=errors,
    )
    path = write_manifest(manifest, base)
    _echo(f"Wrote {path}")
    _echo("Run `ossuary report` to render the HTML report.")


@app.command()
def report(
    out: Path = typer.Option(Path("report.html"), "--out", help="Output HTML file."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the report in a browser when done."
    ),
) -> None:
    """Render the HTML report from artifacts written by `scan`. Runs no inference."""
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


@agents_app.command("test")
def agents_test(
    name: str = typer.Argument(..., help="Agent to test: scanner or clusterer."),
    fixture: Path = typer.Option(..., "--fixture", help="Directory of fixture sessions."),
    config_path: Path | None = typer.Option(None, "--config", help="Path to agents.yaml."),
    live: bool = typer.Option(
        False, "--live", help="Call the real model. Without this, only the prompt assembly is checked."
    ),
) -> None:
    """Exercise an agent against fixture sessions.

    Without `--live` this validates config, parsing, and prompt assembly without
    spending anything -- which is what you want when iterating on a prompt.
    """
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return

    if name not in config.agents:
        _fail(f"unknown agent {name!r}; agents.yaml defines: {', '.join(sorted(config.agents))}")
        return

    agent_config = config.agents[name]
    if not fixture.exists():
        _fail(f"fixture path not found: {fixture}")
        return

    _echo(f"agent: {name}")
    _echo(f"  model: {agent_config.model}")
    _echo(f"  temperature: {agent_config.temperature}")
    _echo(f"  max_turns: {agent_config.max_turns}")
    _echo(f"  prompt_version: {agent_config.prompt_version}")
    _echo(f"  prompt: {len(agent_config.prompt)} chars")
    _echo()

    store = SessionStore()
    refs = store.discover(list(ALL_SOURCES), roots=[fixture])
    if not refs:
        _fail(f"no session files found under {fixture}")
        return

    _echo(f"fixtures: {len(refs)} session(s) under {fixture}")
    sessions = [store.load(ref) for ref in refs]
    stats = compute_tool_stats(sessions)

    for session in sessions:
        outline = store.outline(session.session_id)
        degraded = session.parse_error_count
        _echo(
            f"  {session.session_id}: {len(session.events)} events, "
            f"outline {len(outline):,} chars (~{len(outline) // 4:,} tokens)"
            + (f", {degraded} degraded line(s)" if degraded else "")
        )

    _echo()
    _echo(render_tool_stats(stats, limit=10))

    if not live:
        _echo("Prompt assembly OK. Re-run with --live to call the model.")
        return

    if name == "scanner":
        from .agents.deps import ScannerDeps
        from .agents.scanner import build_scanner_agent, scanner_usage_limits

        agent = build_scanner_agent(agent_config)
        for session in sessions:
            deps = ScannerDeps(
                store=store,
                session_id=session.session_id,
                session_content_hash=session.content_hash,
                tool_stats=stats,
            )
            result = agent.run_sync(
                f"Investigate this session.\n\n{store.outline(session.session_id)}",
                deps=deps,
                usage_limits=scanner_usage_limits(agent_config),
            )
            _echo(f"\n{session.session_id}: {result.usage.requests} turn(s), {len(deps.collected)} issue(s)")
            for issue in deps.collected:
                _echo(f"  [{issue.severity}/{issue.phase}] {issue.title}")
                _echo(f"      events {issue.evidence_event_indices} conf={issue.confidence:.2f}")
    else:
        _fail("live testing of the clusterer needs issues; run `ossuary scan` instead.")


@agents_app.command("show")
def agents_show(
    config_path: Path | None = typer.Option(None, "--config", help="Path to agents.yaml."),
) -> None:
    """Show the resolved agent configuration and where it was loaded from."""
    try:
        path = find_config(config_path)
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return

    _echo(f"config: {path}")
    for name, agent_config in sorted(config.agents.items()):
        _echo(f"\n{name}:")
        _echo(f"  model: {agent_config.model}")
        _echo(f"  temperature: {agent_config.temperature}")
        _echo(f"  max_turns: {agent_config.max_turns}")
        _echo(f"  prompt_version: {agent_config.prompt_version}")


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
