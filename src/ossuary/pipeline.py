"""The scan pipeline.

    discover -> adapters -> normalized events (+ shape records)   [deterministic]
             -> Agent A, once per session                          [LLM]
             -> issues + corpus tool stats                         [deterministic]
             -> Agent B, batched over all issues                   [LLM]
             -> clusters reconciled against the stored taxonomy
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .agents.clusterer import (
    MERGE_NOTE,
    ClusterProposal,
    ProposedCluster,
    batches,
    build_clusterer_agent,
    build_prompt,
    clusterer_usage_limits,
    render_cluster_sets,
)
from .agents.deps import ClustererDeps, ScannerDeps
from .agents.scanner import build_scanner_agent, scanner_usage_limits
from .aggregate import compute_tool_stats, corpus_event_count
from .cache import Cache
from .config import OssuaryConfig
from .models import (
    SCHEMA_VERSION,
    Cluster,
    RunManifest,
    Session,
    SessionScan,
    StoredIssue,
)
from .store import SessionStore
from .taxonomy import Taxonomy

ARTIFACT_DIRNAME = ".ossuary"
MANIFEST_FILENAME = "run.json"


def make_run_id(when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}"


def issue_id_for(session_id: str, index: int, title: str) -> str:
    digest = hashlib.sha256(f"{session_id}|{index}|{title}".encode("utf-8")).hexdigest()[:8]
    return f"{session_id[:8]}-{index:02d}-{digest}"


def scan_session(
    session: Session,
    *,
    store: SessionStore,
    config: OssuaryConfig,
    cache: Cache,
    tool_stats,
    redacted: bool,
) -> SessionScan:
    """Run Agent A over one session, serving from cache when nothing changed."""
    scanner_config = config.scanner
    cache_key = Cache.issues_key(
        session.content_hash,
        scanner_config.prompt_version,
        scanner_config.model,
        schema_version=SCHEMA_VERSION,
        redacted=redacted,
        source=session.source,
    )

    cached = cache.get("issues", cache_key)
    if cached is not None:
        try:
            scan = SessionScan.model_validate(cached)
            scan.from_cache = True
            return scan
        except Exception:  # noqa: BLE001 - a stale entry is a miss, not a failure
            pass

    deps = ScannerDeps(
        store=store,
        session_id=session.session_id,
        session_content_hash=session.content_hash,
        tool_stats=tool_stats,
        cache=cache,
    )
    agent = build_scanner_agent(scanner_config)

    outline = store.outline(session.session_id)
    prompt = (
        f"Investigate this session.\n\n{outline}\n\n"
        f"Call report_issue as soon as you find each issue, then continue. "
        f"When you have accounted for the whole outline, reply with a one-line "
        f"summary of what you found."
    )

    error: str | None = None
    hit_cap = False
    turns = 0
    try:
        result = agent.run_sync(
            prompt, deps=deps, usage_limits=scanner_usage_limits(scanner_config)
        )
        turns = result.usage.requests
    except Exception as exc:  # noqa: BLE001
        # A turn-cap cutoff or provider error still yields whatever the agent
        # reported before it stopped -- that is the whole reason issues are
        # collected incrementally rather than returned at the end.
        error = f"{type(exc).__name__}: {exc}"
        hit_cap = "UsageLimit" in type(exc).__name__

    issues = [
        StoredIssue(
            **issue.model_dump(),
            issue_id=issue_id_for(session.session_id, position, issue.title),
            session_id=session.session_id,
            source=session.source,
            session_path=session.path,
        )
        for position, issue in enumerate(deps.collected)
    ]

    scan = SessionScan(
        session_id=session.session_id,
        source=session.source,
        path=session.path,
        content_hash=session.content_hash,
        issues=issues,
        turns_used=turns,
        hit_turn_cap=hit_cap,
        error=error,
    )
    # Cache partial results too: a run that hit the cap still did real work, and
    # re-paying for it on the next scan of an unchanged file helps nobody.
    cache.set("issues", cache_key, scan.model_dump(mode="json"))
    return scan


def cluster_issues(
    issues: list[StoredIssue],
    tool_stats,
    *,
    config: OssuaryConfig,
    taxonomy: Taxonomy,
    run_id: str,
) -> tuple[list[Cluster], str | None]:
    """Run Agent B, batching only when the issue set is genuinely too large."""
    if not issues:
        return [], None

    clusterer_config = config.clusterer
    agent = build_clusterer_agent(clusterer_config)
    limits = clusterer_usage_limits(clusterer_config)
    known = taxonomy.known()
    issue_lookup = {issue.issue_id: issue.session_id for issue in issues}

    groups = batches(issues)
    proposals: list[ProposedCluster] = []
    error: str | None = None

    try:
        for position, group in enumerate(groups):
            note = (
                f"This is batch {position + 1} of {len(groups)}."
                if len(groups) > 1
                else ""
            )
            prompt = build_prompt(group, tool_stats, known, batch_note=note)
            result = agent.run_sync(
                prompt,
                deps=ClustererDeps(issues=group, tool_stats=tool_stats),
                usage_limits=limits,
            )
            proposals.extend(result.output.clusters)

        if len(groups) > 1 and proposals:
            # Merge pass: independent batches will have named the same failure
            # mode differently, and without reconciliation the report would show
            # the same problem several times over.
            merge_prompt = "\n\n".join(
                [MERGE_NOTE, render_cluster_sets(proposals), build_prompt([], tool_stats, known)]
            )
            merged = agent.run_sync(
                merge_prompt,
                deps=ClustererDeps(issues=issues, tool_stats=tool_stats),
                usage_limits=limits,
            )
            proposals = merged.output.clusters
    except Exception as exc:  # noqa: BLE001
        error = f"clustering failed: {type(exc).__name__}: {exc}"
        if not proposals:
            return [], error

    clusters = taxonomy.reconcile(proposals, run_id=run_id, issue_lookup=issue_lookup)
    return clusters, error


def unclustered_issues(issues: list[StoredIssue], clusters: list[Cluster]) -> list[StoredIssue]:
    """Issues no cluster claimed.

    Surfaced rather than dropped: an issue that vanishes between Agent A and the
    report is indistinguishable from one that was never found.
    """
    claimed = {issue_id for cluster in clusters for issue_id in cluster.member_issue_ids}
    return [issue for issue in issues if issue.issue_id not in claimed]


def artifact_dir(base: Path | None = None) -> Path:
    return (base or Path.cwd()) / ARTIFACT_DIRNAME


def write_manifest(manifest: RunManifest, base: Path | None = None) -> Path:
    directory = artifact_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_FILENAME
    temp = path.with_suffix(".json.tmp")
    temp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    temp.replace(path)

    runs = directory / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{manifest.run_id}.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return path


def read_manifest(base: Path | None = None) -> RunManifest:
    path = artifact_dir(base) / MANIFEST_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"no run artifacts at {path}. Run `ossuary scan` first."
        )
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def corpus_summary(sessions: list[Session]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for session in sessions:
        counts[session.source] = counts.get(session.source, 0) + 1
    return counts


__all__ = [
    "ARTIFACT_DIRNAME",
    "artifact_dir",
    "cluster_issues",
    "corpus_event_count",
    "corpus_summary",
    "issue_id_for",
    "make_run_id",
    "read_manifest",
    "scan_session",
    "unclustered_issues",
    "write_manifest",
]
