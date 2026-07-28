"""Run artifacts: where findings are written and how they are read back.

    discover -> adapters -> normalized events (+ shape records)   [deterministic]
             -> MCP tools, driven by the host agent               [the agent]
             -> issues + clusters, recorded through tools
             -> reconciled against the stored taxonomy
             -> .ossuary/run.json  ->  ossuary report

Everything in this module is deterministic. No model is involved: Ossuary serves
transcripts to whichever agent is already running and writes down what that
agent found. The inference happens on the other side of the MCP boundary.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .models import Cluster, RunManifest, Session, StoredIssue

ARTIFACT_DIRNAME = ".ossuary"
MANIFEST_FILENAME = "run.json"


def make_run_id(when: datetime | None = None) -> str:
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}"


def issue_id_for(session_id: str, index: int, title: str) -> str:
    digest = hashlib.sha256(f"{session_id}|{index}|{title}".encode("utf-8")).hexdigest()[:8]
    return f"{session_id[:8]}-{index:02d}-{digest}"


def unclustered_issues(issues: list[StoredIssue], clusters: list[Cluster]) -> list[StoredIssue]:
    """Issues no cluster claimed.

    Surfaced rather than dropped: an issue that vanishes between being found and
    being reported is indistinguishable from one that was never found.
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
            f"no run artifacts at {path}. Investigate some sessions first: ask "
            f"Claude Code or Copilot CLI to look at your transcripts with the "
            f"Ossuary plugin, then have it call ossuary_write_run."
        )
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def corpus_summary(sessions: list[Session]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for session in sessions:
        counts[session.source] = counts.get(session.source, 0) + 1
    return counts


__all__ = [
    "ARTIFACT_DIRNAME",
    "MANIFEST_FILENAME",
    "artifact_dir",
    "corpus_summary",
    "issue_id_for",
    "make_run_id",
    "read_manifest",
    "unclustered_issues",
    "write_manifest",
]
