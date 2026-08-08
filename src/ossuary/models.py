"""Core data model.

`NormalizedEvent` is the seam between adapters and everything downstream. Agent A
never sees a raw transcript line -- only this. A transcript format change should
touch exactly one adapter and nothing else.

The schema is versioned (`SCHEMA_VERSION`) so that upstream CLI churn becomes a
backfill of stored artifacts rather than a migration of live code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Bump when the *normalized* shape changes. Cached artifacts recording an older
# version are re-derived rather than trusted.
SCHEMA_VERSION = 1

Source = Literal["claude-code", "codex", "copilot", "pi"]
Role = Literal["user", "assistant", "system", "unknown"]
Kind = Literal[
    "message", "tool_call", "tool_result", "thinking", "meta", "unparseable"
]
Phase = Literal["prompt", "tool", "model", "harness", "user", "unknown"]
Severity = Literal["low", "medium", "high"]


class ShapeRecord(BaseModel):
    """Measurements of a tool result payload.

    This is instrumentation, not detection. It measures the payload and lets the
    agent interpret it. Most tool-layer infrastructure problems are far more
    visible here than in the payload text: an exit code of 0 with an empty body
    and a 30-second duration is a timeout swallowed by the harness, and no amount
    of reading the payload shows that as clearly as those three fields together.
    """

    byte_length: int
    duration_ms: int | None = None
    exit_code: int | None = None
    has_error_field: bool = False
    terminates_cleanly: bool = True
    is_round_number: bool = False
    content_hash: str = ""
    is_empty: bool = False

    # Provenance for duration_ms. Some CLIs record it directly; where they do not
    # we derive it from the timestamp delta between the call and its result.
    # Recorded so the agent is never misled about measurement quality.
    duration_source: Literal["recorded", "derived", "unavailable"] = "unavailable"


class NormalizedEvent(BaseModel):
    """One atomic thing that happened in a session.

    A single transcript line may expand to several events (a message with a text
    block and two tool_use blocks is three events). `index` is the ordinal over
    *events*, not lines, and is stable for a given file content.
    """

    session_id: str
    source: Source
    index: int
    ts: datetime | None = None
    role: Role = "unknown"
    kind: Kind = "message"
    tool_name: str | None = None
    text: str = ""
    shape: ShapeRecord | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    raw: str | None = None
    parse_error: str | None = None


class SessionRef(BaseModel):
    """A session discovered on disk, before it is read."""

    session_id: str
    source: Source
    path: str
    size_bytes: int
    mtime: datetime | None = None
    project: str | None = None


class Session(BaseModel):
    """A fully normalized session."""

    session_id: str
    source: Source
    path: str
    events: list[NormalizedEvent] = Field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    content_hash: str = ""
    parse_error_count: int = 0
    project: str | None = None

    def by_index(self, index: int) -> NormalizedEvent | None:
        if 0 <= index < len(self.events) and self.events[index].index == index:
            return self.events[index]
        for e in self.events:
            if e.index == index:
                return e
        return None


class Issue(BaseModel):
    """Agent A's output. Open schema, rigid envelope.

    Deliberately carries no taxonomy of known issue types: `title` and
    `description` are the agent's own words. Handing the agent a menu of expected
    failure modes would launder hardcoded assumptions back in through the prompt
    and destroy the discovery property that is the entire point of the tool.
    """

    title: str
    description: str
    severity: Severity
    phase: Phase
    evidence_event_indices: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class StoredIssue(Issue):
    """An `Issue` plus the provenance the pipeline needs to reference it."""

    issue_id: str
    session_id: str
    source: Source
    session_path: str = ""


class ProposedCluster(BaseModel):
    """One cluster as the investigating agent proposes it, before ids are assigned.

    Separate from `Cluster` because the agent does not get to choose a
    `cluster_id`: that is the taxonomy's job, and it is what keeps names stable
    between runs. The agent may only claim an existing id, never mint one.
    """

    name: str = Field(description="Short human-readable name for this failure mode")
    summary: str = Field(description="What these issues have in common and why it matters")
    member_issue_ids: list[str] = Field(
        default_factory=list, description="issue_id values belonging to this cluster"
    )
    existing_cluster_id: str | None = Field(
        default=None,
        description=(
            "If this matches a cluster from the stored taxonomy, its cluster_id. "
            "Null if this is a genuinely new failure mode."
        ),
    )


class Cluster(BaseModel):
    cluster_id: str
    name: str
    summary: str
    member_issue_ids: list[str] = Field(default_factory=list)
    affected_sessions: list[str] = Field(default_factory=list)
    first_seen_run: str = ""
    is_new_this_run: bool = False


class ToolStats(BaseModel):
    """Corpus-wide aggregates for one tool.

    Agent A structurally cannot see these -- a server that returns exactly 30000
    bytes on 40% of calls is unremarkable within any single session and damning
    across two hundred. Agent B can only see it if we compute it here.
    """

    tool_name: str
    call_count: int = 0
    session_count: int = 0
    error_count: int = 0
    empty_count: int = 0
    round_number_count: int = 0
    truncated_looking_count: int = 0
    duplicate_result_count: int = 0
    distinct_result_hashes: int = 0
    byte_length_min: int = 0
    byte_length_max: int = 0
    byte_length_p50: int = 0
    byte_length_p95: int = 0
    duration_ms_p50: int | None = None
    duration_ms_p95: int | None = None
    duration_ms_max: int | None = None
    top_byte_lengths: list[tuple[int, int]] = Field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.error_count / self.call_count if self.call_count else 0.0

    @property
    def duplicate_rate(self) -> float:
        return self.duplicate_result_count / self.call_count if self.call_count else 0.0


class SessionScan(BaseModel):
    """What one session contributed to a run."""

    session_id: str
    source: Source
    path: str
    content_hash: str
    issues: list[StoredIssue] = Field(default_factory=list)
    error: str | None = None


class RunManifest(BaseModel):
    """Everything `report` needs, written by `ossuary_write_run`."""

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    schema_version: int = SCHEMA_VERSION

    #: Who did the investigating, in their own words. Ossuary cannot know this
    #: -- it serves transcripts to a host agent and never sees which model is on
    #: the other end -- so the agent names itself, or this stays blank. Recorded
    #: rather than inferred: a report that guessed would be worse than one that
    #: admits it does not know.
    investigator: str = ""

    redaction_enabled: bool = True
    session_count: int = 0
    event_count: int = 0
    issue_count: int = 0
    sources: dict[str, int] = Field(default_factory=dict)
    scans: list[SessionScan] = Field(default_factory=list)
    tool_stats: list[ToolStats] = Field(default_factory=list)
    clusters: list[Cluster] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
