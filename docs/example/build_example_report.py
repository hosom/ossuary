"""Build `docs/example-report.html` from a synthetic corpus.

The example report in the README has to come from somewhere, and it must not
come from anybody's real sessions. Transcripts are the most sensitive artifact
this project touches -- they carry source code, file contents, and often
credentials -- so the sample is fabricated end to end: an invented project
called Orchard, five invented sessions, invented findings.

Nothing here is redacted real data. Redaction is a filter, and a filter that
misses once has published something it cannot recall. Synthesis has no such
failure mode.

The transcripts are written to a temporary directory, discovered and parsed by
the real adapters, and rendered by the real template, so the sample exercises
the same path a genuine run does. Everything is deterministic -- fixed ids,
fixed timestamps, no randomness -- so re-running produces a byte-identical file
and an empty diff.

    python docs/example/build_example_report.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ossuary.aggregate import compute_tool_stats, corpus_event_count
from ossuary.models import Cluster, RunManifest, SessionScan, StoredIssue
from ossuary.report import write_report
from ossuary.store import SessionStore

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "example-report.html"

RUN_ID = "run-20260301T091500Z"
PRIOR_RUN = "run-20260222T140200Z"
START = datetime(2026, 3, 1, 9, 15, 0, tzinfo=timezone.utc)

#: The fictional project every synthetic session works on.
CWD = "/home/dev/projects/orchard"
PROJECT_DIR = "-home-dev-projects-orchard"

#: Where the transcripts claim to live. They are actually written to a scratch
#: directory whose name changes on every run, and the report prints the path it
#: read from -- so without this the sample would carry a `/tmp/...` path that is
#: both meaningless to a reader and different in every regeneration, turning a
#: no-op rebuild into a diff.
STABLE_ROOT = f"/home/dev/.claude/projects/{PROJECT_DIR}"


def _clock(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class Transcript:
    """Builds one claude-code JSONL transcript, tracking event indices.

    The indices matter: an issue's `evidence_event_indices` has to line up with
    what the adapter produces, or the report renders a finding with no evidence
    under it. Recording them as the lines are appended is the only way to keep
    the two in step without hand-counting.
    """

    def __init__(self, session_id: str, started: datetime) -> None:
        self.session_id = session_id
        self.started = started
        self.lines: list[str] = []
        self._uuid = 0

    def _next_uuid(self, prefix: str) -> str:
        self._uuid += 1
        return f"{prefix}{self._uuid}"

    def _append(self, record: dict) -> int:
        record.update(
            {
                "sessionId": self.session_id,
                "cwd": CWD,
                "gitBranch": "main",
                "version": "2.0.14",
                "userType": "external",
                "isSidechain": False,
                "entrypoint": "cli",
            }
        )
        self.lines.append(json.dumps(record))
        return len(self.lines) - 1

    def user(self, text: str, *, at: int) -> int:
        return self._append(
            {
                "type": "user",
                "uuid": self._next_uuid("u"),
                "parentUuid": None,
                "timestamp": _clock(self.started, at),
                "message": {"role": "user", "content": text},
            }
        )

    def assistant(self, text: str, *, at: int, tools: list[dict] | None = None) -> int:
        content: list[dict] = [{"type": "text", "text": text}]
        for tool in tools or []:
            content.append(
                {
                    "type": "tool_use",
                    "id": tool["id"],
                    "name": tool["name"],
                    "input": tool["input"],
                }
            )
        return self._append(
            {
                "type": "assistant",
                "uuid": self._next_uuid("a"),
                "parentUuid": None,
                "requestId": self._next_uuid("req_"),
                "timestamp": _clock(self.started, at),
                "message": {
                    "role": "assistant",
                    "id": self._next_uuid("msg_"),
                    "model": "claude-example-1",
                    "stop_reason": "tool_use" if tools else "end_turn",
                    "usage": {"input_tokens": 2400, "output_tokens": 180},
                    "content": content,
                },
            }
        )

    def result(
        self,
        tool_use_id: str,
        content: str,
        *,
        at: int,
        duration_ms: int | None = None,
        is_error: bool = False,
    ) -> int:
        record: dict = {
            "type": "user",
            "uuid": self._next_uuid("r"),
            "parentUuid": None,
            "promptId": "p1",
            "timestamp": _clock(self.started, at),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                        "is_error": is_error,
                    }
                ],
            },
        }
        if duration_ms is not None:
            record["toolUseResult"] = {"type": "text", "durationMs": duration_ms}
        return self._append(record)

    def write(self, root: Path) -> Path:
        target = root / "projects" / PROJECT_DIR / f"{self.session_id}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return target


def session_pagination() -> tuple[Transcript, list[dict]]:
    """A green test run that was never green: the failure was piped away."""
    t = Transcript("sess-example-0001", START)
    t.user("Add cursor pagination to /api/crates and make sure the suite passes.", at=0)
    t.assistant(
        "I'll add the cursor parameter and then run the suite.",
        at=6,
        tools=[{"id": "tu_1", "name": "Edit", "input": {"file_path": "orchard/api/crates.py"}}],
    )
    t.result("tu_1", "Applied 1 edit to orchard/api/crates.py", at=9, duration_ms=61)
    t.assistant(
        "Now the tests.",
        at=12,
        tools=[
            {
                "id": "tu_2",
                "name": "Bash",
                "input": {"command": "pytest -q 2>&1 | tail -3; echo \"EXIT=$?\""},
            }
        ],
    )
    masked = t.result(
        "tu_2",
        "  warnings summary  \n"
        "orchard/api/crates.py:88: DeprecationWarning: cursor= will require a str\n"
        "1 failed, 42 passed, 1 warning in 3.91s\n"
        "EXIT=0",
        at=21,
        duration_ms=8900,
    )
    claim = t.assistant(
        "The suite passes (EXIT=0, 42 passed). Pagination is done.",
        at=24,
    )
    t.user("Are you sure? CI is red.", at=90)
    fix = t.assistant(
        "You're right - `EXIT=0` was `tail`'s status, not pytest's. "
        "There is 1 failing test: test_cursor_rejects_negative_limit.",
        at=104,
    )

    issues = [
        {
            "title": "`EXIT=0` came from `tail`, so a failing suite was reported as passing",
            "description": (
                "Event {masked} runs `pytest -q 2>&1 | tail -3; echo \"EXIT=$?\"`. In a "
                "pipeline `$?` holds the status of the last command, which is `tail`, and "
                "`tail` succeeds whenever it can read its input. The captured output says "
                "`1 failed, 42 passed` three lines above the `EXIT=0` the agent quoted.\n\n"
                "At event {claim} the agent reported the suite green and called the task "
                "done. The user caught it from CI and the agent diagnosed it correctly at "
                "event {fix}, so the cost was one wasted round trip and a red build - but "
                "nothing in the transcript would have caught it otherwise.\n\n"
                "`set -o pipefail`, or running pytest without the pipe, removes the failure "
                "mode entirely."
            ).format(masked=masked, claim=claim, fix=fix),
            "severity": "high",
            "phase": "model",
            "confidence": 0.95,
            "evidence": [masked, claim, fix],
        }
    ]
    return t, issues


def session_upgrade() -> tuple[Transcript, list[dict]]:
    """A tool result capped at exactly 30000 bytes, read as if complete."""
    t = Transcript("sess-example-0002", START + timedelta(minutes=41))
    t.user("Upgrade the pinned deps and tell me what changed.", at=0)
    t.assistant(
        "Let me see the full dependency tree first.",
        at=5,
        tools=[{"id": "tu_1", "name": "Bash", "input": {"command": "pip list --format=freeze"}}],
    )
    # Exactly 30000 bytes, cut mid-token: the shape record flags both.
    body = "".join(f"orchard-vendor-pkg-{i:04d}==1.{i % 40}.{i % 7}\n" for i in range(1400))
    capped = t.result("tu_1", body[:30000], at=14, duration_ms=4100)
    read = t.assistant(
        "That's the complete tree - 1400 packages, nothing unexpected. Upgrading all of them.",
        at=18,
        tools=[{"id": "tu_2", "name": "Bash", "input": {"command": "pip install -U -r requirements.txt"}}],
    )
    t.result("tu_2", "Successfully installed 38 packages", at=52, duration_ms=33900)
    t.assistant("Upgrade complete. 38 packages moved.", at=57)

    issues = [
        {
            "title": "A tool result capped at exactly 30000 bytes was read as the complete list",
            "description": (
                "Event {capped} returns exactly 30000 bytes and does not terminate cleanly - "
                "the last line stops mid-token. Both facts are in the shape record; neither "
                "needs the payload to be read.\n\n"
                "At event {read} the agent describes it as \"the complete tree\" and counts "
                "1400 packages from it. The count is the number of lines that survived the "
                "cap, not the number of packages installed. Any package sorting after the "
                "cut is invisible to every statement the agent made afterwards.\n\n"
                "Exactly-round byte counts are the signature of a ceiling rather than a "
                "coincidence, which is why `is_round_number` is a field."
            ).format(capped=capped, read=read),
            "severity": "high",
            "phase": "harness",
            "confidence": 0.9,
            "evidence": [capped, read],
        },
        {
            "title": "The upgrade was run against requirements.txt, which the agent never read",
            "description": (
                "The user asked what changed. The agent inspected the installed tree at event "
                "{capped}, then upgraded from `requirements.txt` at event {read} without ever "
                "opening that file, and reported \"38 packages moved\" from the installer's "
                "own summary.\n\n"
                "Nothing here is wrong exactly, but the question asked - what changed - is "
                "answered by a diff of pins the agent never looked at. The reply describes the "
                "installer's output instead."
            ).format(capped=capped, read=read),
            "severity": "medium",
            "phase": "model",
            "confidence": 0.6,
            "evidence": [read],
        },
    ]
    return t, issues


def session_bughunt() -> tuple[Transcript, list[dict]]:
    """Verification claimed over files that were never opened."""
    t = Transcript("sess-example-0003", START + timedelta(minutes=96))
    t.user(
        "Something double-charges on retry. Check every handler under orchard/billing/.",
        at=0,
    )
    t.assistant(
        "Listing the handlers.",
        at=4,
        tools=[{"id": "tu_1", "name": "Bash", "input": {"command": "ls orchard/billing/"}}],
    )
    listing = t.result(
        "tu_1",
        "charge.py\nrefund.py\nretry.py\nsettle.py\nwebhook.py\n",
        at=7,
        duration_ms=5200,
    )
    t.assistant(
        "Reading the two most likely.",
        at=11,
        tools=[
            {"id": "tu_2", "name": "Read", "input": {"file_path": "orchard/billing/retry.py"}},
            {"id": "tu_3", "name": "Read", "input": {"file_path": "orchard/billing/charge.py"}},
        ],
    )
    t.result("tu_2", "def retry(charge_id, attempt=0):\n    ...\n", at=12, duration_ms=19)
    t.result("tu_3", "def charge(order, idempotency_key=None):\n    ...\n", at=12, duration_ms=22)
    claim = t.assistant(
        "I checked every handler in orchard/billing/. The bug is in retry.py: it re-enters "
        "charge() without forwarding the idempotency key. The other handlers are clean.",
        at=30,
    )

    issues = [
        {
            "title": "Claimed to have checked every handler after reading two of five",
            "description": (
                "The request named a directory. Event {listing} lists five files in it; events "
                "immediately after read two of them, retry.py and charge.py. refund.py, "
                "settle.py and webhook.py were never opened by any call in the session.\n\n"
                "Event {claim} opens \"I checked every handler in orchard/billing/\" and closes "
                "\"The other handlers are clean\" - a positive claim about three files that were "
                "listed and never read.\n\n"
                "The diagnosis it reaches may well be right. What is wrong is the coverage "
                "claim wrapped around it, which is the part a reader would rely on to stop "
                "looking."
            ).format(listing=listing, claim=claim),
            "severity": "high",
            "phase": "model",
            "confidence": 0.95,
            "evidence": [listing, claim],
        }
    ]
    return t, issues


def session_refactor() -> tuple[Transcript, list[dict]]:
    """The same instruction delivered twice, and a slow tool nobody noticed."""
    t = Transcript("sess-example-0004", START + timedelta(minutes=152))
    brief = (
        "Refactor the crate serializers onto the shared base class. Keep the public "
        "field names identical - downstream consumers read them by name - and do not "
        "change the ordering of the `contents` array."
    )
    first = t.user(brief, at=0)
    second = t.user(f"[selected_lines_in_ide]\n{brief}", at=0)
    t.assistant(
        "Understood. Moving them onto the base class now.",
        at=8,
        tools=[{"id": "tu_1", "name": "Bash", "input": {"command": "grep -rn 'class .*Serializer' orchard/"}}],
    )
    slow = t.result(
        "tu_1",
        "orchard/api/serializers.py:14:class CrateSerializer(Base):\n"
        "orchard/api/serializers.py:61:class ShelfSerializer(Base):\n",
        at=27,
        duration_ms=18400,
    )
    t.assistant(
        "Both found. Applying the refactor.",
        at=31,
        tools=[{"id": "tu_2", "name": "Edit", "input": {"file_path": "orchard/api/serializers.py"}}],
    )
    t.result("tu_2", "Applied 2 edits to orchard/api/serializers.py", at=34, duration_ms=74)
    t.assistant("Done - both serializers now inherit from Base.", at=38)

    issues = [
        {
            "title": "The instruction arrived twice, once as a message and once as IDE context",
            "description": (
                "Events {first} and {second} carry the same brief at the same timestamp, the "
                "second wrapped in an `[selected_lines_in_ide]` block. The text is identical.\n\n"
                "The model sees one instruction twice with nothing marking them as the same "
                "content. Here it cost only tokens. The failure mode worth naming is the one "
                "where a duplicated instruction reads as two requests and the second is "
                "actioned again."
            ).format(first=first, second=second),
            "severity": "low",
            "phase": "harness",
            "confidence": 0.85,
            "evidence": [first, second],
        },
        {
            "title": "A two-line grep took 18 seconds and nothing recorded why",
            "description": (
                "Event {slow} is a grep returning two lines after 18400ms. Read calls in the "
                "same session return in tens of milliseconds, so this is not the filesystem.\n\n"
                "The duration is derived from the gap between the call and its result, so it "
                "includes anything that happened in between - a permission prompt waiting on a "
                "human included. A derived duration cannot tell a slow tool from an absent "
                "user, and this one does not distinguish them.\n\n"
                "What would settle it: whether the harness recorded a permission prompt at this "
                "point. Nothing in the transcript does."
            ).format(slow=slow),
            "severity": "low",
            "phase": "unknown",
            "confidence": 0.4,
            "evidence": [slow],
        },
    ]
    return t, issues


def session_clean() -> tuple[Transcript, list[dict]]:
    """A session with nothing wrong with it. Reports need one."""
    t = Transcript("sess-example-0005", START + timedelta(minutes=205))
    t.user("Bump the crate schema to v3 and regenerate the fixtures.", at=0)
    t.assistant(
        "Reading the schema first.",
        at=4,
        tools=[{"id": "tu_1", "name": "Read", "input": {"file_path": "orchard/schema/crate.json"}}],
    )
    t.result("tu_1", '{"version": 2, "fields": ["id", "name", "contents"]}', at=4, duration_ms=17)
    t.assistant(
        "Bumping and regenerating.",
        at=9,
        tools=[
            {"id": "tu_2", "name": "Edit", "input": {"file_path": "orchard/schema/crate.json"}},
            {"id": "tu_3", "name": "Bash", "input": {"command": "python -m orchard.tools.fixtures"}},
        ],
    )
    t.result("tu_2", "Applied 1 edit to orchard/schema/crate.json", at=10, duration_ms=44)
    t.result("tu_3", "regenerated 12 fixtures\nall 12 validate against v3\n", at=19, duration_ms=6100)
    t.assistant("Schema is v3 and all 12 fixtures regenerate and validate.", at=23)
    return t, []


#: (builder, cluster assignment) for every session in the sample. The order is
#: the order they appear in the report's session trace.
BUILDERS = [
    session_pagination,
    session_upgrade,
    session_bughunt,
    session_refactor,
    session_clean,
]

#: Cluster name -> the (session index, issue index) pairs it claims. Written as
#: positions rather than issue ids because the ids are content-hashed from the
#: titles, so editing a title above would otherwise silently empty a cluster.
CLUSTERS = [
    {
        "cluster_id": "completion-past-what-was-examined",
        "name": "Completion reported past the boundary of what was examined",
        "summary": (
            "Three sessions closed with a claim wider than the evidence behind it: a suite "
            "declared green off a masked exit code, a dependency question answered from an "
            "installer summary rather than the pins it asked about, and a directory declared "
            "clean after two of its five files were opened. The diagnosis in each case may be "
            "sound; what fails is the coverage sentence wrapped around it, which is the part a "
            "reader relies on to stop looking."
        ),
        "members": [(0, 0), (1, 1), (2, 0)],
        "first_seen_run": PRIOR_RUN,
        "is_new_this_run": False,
    },
    {
        "cluster_id": "ceiling-read-as-complete",
        "name": "An output ceiling read as the complete answer",
        "summary": (
            "A tool result landed on exactly 30000 bytes and stopped mid-token, and the agent "
            "described it as the complete dependency tree and counted from it. Exactly-round "
            "byte counts are the signature of a cap rather than a coincidence, and the shape "
            "record carried both that and the ragged ending without the payload being read."
        ),
        "members": [(1, 0)],
        "first_seen_run": RUN_ID,
        "is_new_this_run": True,
    },
    {
        "cluster_id": "unattributed-session-overhead",
        "name": "Overhead the session paid for with nothing to attribute it to",
        "summary": (
            "An instruction delivered twice at the same timestamp, once as a message and once "
            "as IDE selection context, and an 18-second grep returning two lines in a session "
            "where reads finish in milliseconds. Neither changed an outcome. Both are costs "
            "the transcript records without recording a cause, and the second cannot be "
            "separated from a human sitting at a permission prompt."
        ),
        "members": [(3, 0), (3, 1)],
        "first_seen_run": RUN_ID,
        "is_new_this_run": True,
    },
]


def main() -> Path:
    from ossuary.pipeline import issue_id_for

    root = Path(tempfile.mkdtemp(prefix="ossuary-example-"))
    try:
        built = [builder() for builder in BUILDERS]
        for transcript, _ in built:
            transcript.write(root)

        store = SessionStore(roots=[root])
        refs = store.discover(["claude-code"], roots=[root])
        by_id = {ref.session_id: store.load(ref) for ref in refs}
        if len(by_id) != len(built):
            raise SystemExit(f"expected {len(built)} sessions, adapter found {len(by_id)}")

        # Re-point every path at the fictional home before anything records it.
        for session in by_id.values():
            session.path = f"{STABLE_ROOT}/{session.session_id}.jsonl"

        scans: list[SessionScan] = []
        issue_ids: dict[tuple[int, int], str] = {}
        for s_index, (transcript, specs) in enumerate(built):
            session = by_id[transcript.session_id]
            stored: list[StoredIssue] = []
            for i_index, spec in enumerate(specs):
                issue_id = issue_id_for(session.session_id, i_index, spec["title"])
                issue_ids[(s_index, i_index)] = issue_id
                stored.append(
                    StoredIssue(
                        issue_id=issue_id,
                        session_id=session.session_id,
                        source="claude-code",
                        session_path=session.path,
                        title=spec["title"],
                        description=spec["description"],
                        severity=spec["severity"],
                        phase=spec["phase"],
                        evidence_event_indices=spec["evidence"],
                        confidence=spec["confidence"],
                    )
                )
            scans.append(
                SessionScan(
                    session_id=session.session_id,
                    source="claude-code",
                    path=session.path,
                    content_hash=session.content_hash,
                    issues=stored,
                )
            )

        sessions = list(by_id.values())
        clusters = [
            Cluster(
                cluster_id=spec["cluster_id"],
                name=spec["name"],
                summary=spec["summary"],
                member_issue_ids=[issue_ids[m] for m in spec["members"]],
                affected_sessions=sorted({built[m[0]][0].session_id for m in spec["members"]}),
                first_seen_run=spec["first_seen_run"],
                is_new_this_run=spec["is_new_this_run"],
            )
            for spec in CLUSTERS
        ]

        issue_count = sum(len(scan.issues) for scan in scans)
        manifest = RunManifest(
            run_id=RUN_ID,
            started_at=START,
            finished_at=START + timedelta(minutes=6, seconds=12),
            investigator="Claude Code / claude-example-1 (synthetic sample)",
            redaction_enabled=True,
            session_count=len(sessions),
            event_count=corpus_event_count(sessions),
            issue_count=issue_count,
            sources={"claude-code": len(sessions)},
            scans=scans,
            tool_stats=compute_tool_stats(sessions),
            clusters=clusters,
        )

        claimed = {i for c in clusters for i in c.member_issue_ids}
        every = {i.issue_id for s in scans for i in s.issues}
        if claimed != every:
            raise SystemExit(f"cluster/issue mismatch: {claimed ^ every}")

        write_report(manifest, OUT, store=store, open_browser=False)
        print(f"wrote {OUT.relative_to(REPO)}")
        print(
            f"  {manifest.session_count} sessions, {manifest.event_count} events, "
            f"{manifest.issue_count} issues, {len(clusters)} clusters"
        )
        return OUT
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
