"""Persistent cluster taxonomy at `.ossuary/taxonomy.json`.

Named clusters survive between runs so that later runs assign to existing
clusters where they fit and only propose genuinely new ones. Three things fall
out of that: reports stop reshuffling between runs, re-scans cost less, and
"new issue types this run" becomes a real signal instead of an artifact of the
model picking different words for the same failure mode.

Cluster ids are derived from the cluster's name, so the same failure mode keeps
its id across runs even if its summary is rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import Cluster, ProposedCluster

TAXONOMY_FILENAME = "taxonomy.json"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug[:48] or "unnamed"


def cluster_id_for(name: str) -> str:
    """Stable id derived from the name.

    A short hash is appended so two different names that slugify identically do
    not collide into one cluster.
    """
    digest = hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()[:6]
    return f"{slugify(name)}-{digest}"


class Taxonomy:
    """Load, reconcile, and persist the known cluster set."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.entries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # A corrupt taxonomy must not fail a run. Losing continuity is bad;
            # losing the run is worse.
            self.entries = {}
            return
        clusters = raw.get("clusters") if isinstance(raw, dict) else raw
        if not isinstance(clusters, list):
            self.entries = {}
            return
        self.entries = {
            str(entry["cluster_id"]): entry
            for entry in clusters
            if isinstance(entry, dict) and entry.get("cluster_id")
        }

    def known(self) -> list[dict]:
        return list(self.entries.values())

    def reconcile(
        self,
        proposals: list[ProposedCluster],
        *,
        run_id: str,
        issue_lookup: dict[str, str],
    ) -> list[Cluster]:
        """Turn proposals into `Cluster`s, matched against the stored taxonomy.

        A proposal matches an existing cluster when the model says so via
        `existing_cluster_id`, or when its name resolves to an id we already
        hold. `is_new_this_run` is true only when neither applies.

        Args:
            issue_lookup: issue_id -> session_id, so affected sessions are
                computed from the pipeline's own records rather than trusted
                from model output.
        """
        clusters: list[Cluster] = []
        used_ids: set[str] = set()

        for proposal in proposals:
            name = (proposal.name or "").strip() or "Unnamed cluster"
            claimed = (proposal.existing_cluster_id or "").strip()
            derived = cluster_id_for(name)

            if claimed and claimed in self.entries:
                cluster_id = claimed
                is_new = False
            elif derived in self.entries:
                cluster_id = derived
                is_new = False
            else:
                cluster_id = derived
                is_new = True

            # Two proposals landing on one id would silently merge; suffix
            # instead so both survive and the collision is visible.
            if cluster_id in used_ids:
                suffix = 2
                while f"{cluster_id}-{suffix}" in used_ids:
                    suffix += 1
                cluster_id = f"{cluster_id}-{suffix}"
                is_new = cluster_id not in self.entries
            used_ids.add(cluster_id)

            members = [i for i in proposal.member_issue_ids if i in issue_lookup]
            sessions = sorted({issue_lookup[i] for i in members})
            first_seen = (
                self.entries.get(cluster_id, {}).get("first_seen_run") or run_id
            )

            clusters.append(
                Cluster(
                    cluster_id=cluster_id,
                    name=name,
                    summary=(proposal.summary or "").strip(),
                    member_issue_ids=members,
                    affected_sessions=sessions,
                    first_seen_run=first_seen,
                    is_new_this_run=is_new,
                )
            )

        clusters.sort(key=lambda c: (-len(c.affected_sessions), c.name))
        return clusters

    def update(self, clusters: list[Cluster], *, run_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for cluster in clusters:
            existing = self.entries.get(cluster.cluster_id, {})
            self.entries[cluster.cluster_id] = {
                "cluster_id": cluster.cluster_id,
                "name": cluster.name,
                "summary": cluster.summary,
                "first_seen_run": existing.get("first_seen_run") or cluster.first_seen_run or run_id,
                "last_seen_run": run_id,
                "last_updated": now,
                "total_runs_seen": int(existing.get("total_runs_seen") or 0) + 1,
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "clusters": sorted(self.entries.values(), key=lambda e: e["cluster_id"]),
        }
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)
