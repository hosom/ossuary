from __future__ import annotations

import json

import pytest
import yaml

from ossuary.cache import Cache
from ossuary.config import load_config
from ossuary.models import Cluster
from ossuary.taxonomy import Taxonomy, cluster_id_for

VALID = {
    "agents": {
        "scanner": {"model": "anthropic:claude-haiku-4-5", "temperature": 0, "max_turns": 15, "prompt": "look"},
        "clusterer": {"model": "anthropic:claude-sonnet-5", "temperature": 0, "max_turns": 3, "prompt": "group"},
    }
}


def _write(tmp_path, data):
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class TestConfig:
    def test_valid_config_loads(self, tmp_path):
        config = load_config(_write(tmp_path, VALID))
        assert config.scanner.model == "anthropic:claude-haiku-4-5"
        assert config.clusterer.max_turns == 3

    def test_unknown_keys_fail_loudly(self, tmp_path):
        data = json.loads(json.dumps(VALID))
        data["agents"]["scanner"]["temprature"] = 0.5  # typo
        with pytest.raises(Exception, match="[Ee]xtra|forbid"):
            load_config(_write(tmp_path, data))

    def test_missing_required_agent_fails(self, tmp_path):
        data = {"agents": {"scanner": VALID["agents"]["scanner"]}}
        with pytest.raises(Exception, match="clusterer"):
            load_config(_write(tmp_path, data))

    def test_empty_prompt_fails(self, tmp_path):
        data = json.loads(json.dumps(VALID))
        data["agents"]["scanner"]["prompt"] = "   "
        with pytest.raises(Exception):
            load_config(_write(tmp_path, data))

    def test_out_of_range_values_fail(self, tmp_path):
        data = json.loads(json.dumps(VALID))
        data["agents"]["scanner"]["max_turns"] = 0
        with pytest.raises(Exception):
            load_config(_write(tmp_path, data))

    def test_prompt_version_tracks_only_the_prompt(self, tmp_path):
        """A prompt edit must invalidate inference; a model swap must not look like one."""
        first = load_config(_write(tmp_path, VALID)).scanner.prompt_version

        same = json.loads(json.dumps(VALID))
        same["agents"]["scanner"]["model"] = "ollama:qwen"
        assert load_config(_write(tmp_path, same)).scanner.prompt_version == first

        changed = json.loads(json.dumps(VALID))
        changed["agents"]["scanner"]["prompt"] = "look harder"
        assert load_config(_write(tmp_path, changed)).scanner.prompt_version != first

    def test_the_shipped_config_is_valid(self):
        from pathlib import Path

        config = load_config(Path(__file__).parent.parent / "agents.yaml")
        assert config.scanner.prompt and config.clusterer.prompt

    def test_the_shipped_prompt_hands_the_agent_no_taxonomy(self):
        """Section 1: a menu of known failure modes would destroy discovery."""
        from pathlib import Path

        prompt = load_config(Path(__file__).parent.parent / "agents.yaml").scanner.prompt.lower()
        assert "in your own words" in prompt
        assert "no list of things to look for" in prompt


class TestCache:
    def test_roundtrip(self, tmp_path):
        cache = Cache(tmp_path)
        cache.set("issues", "k1", {"a": 1})
        assert cache.get("issues", "k1") == {"a": 1}

    def test_miss_returns_none(self, tmp_path):
        assert Cache(tmp_path).get("issues", "absent") is None

    def test_disabled_cache_never_serves_or_writes(self, tmp_path):
        cache = Cache(tmp_path, enabled=False)
        cache.set("issues", "k", {"a": 1})
        assert cache.get("issues", "k") is None

    def test_corrupt_entry_is_a_miss_not_an_error(self, tmp_path):
        cache = Cache(tmp_path)
        cache.set("issues", "k", {"a": 1})
        path = cache._path("issues", "k")
        path.write_text("{not json", encoding="utf-8")
        assert cache.get("issues", "k") is None

    def test_tool_key_depends_on_file_content_and_args(self):
        base = Cache.tool_key("hash1", "read_events", {"start": 0, "end": 5})
        assert base == Cache.tool_key("hash1", "read_events", {"end": 5, "start": 0})
        assert base != Cache.tool_key("hash2", "read_events", {"start": 0, "end": 5})
        assert base != Cache.tool_key("hash1", "read_events", {"start": 1, "end": 5})

    def test_issue_key_changes_with_prompt_and_model(self):
        args = dict(schema_version=1, redacted=True)
        base = Cache.issues_key("h", "p1", "m1", **args)
        assert base != Cache.issues_key("h", "p2", "m1", **args)
        assert base != Cache.issues_key("h", "p1", "m2", **args)
        assert base != Cache.issues_key("h2", "p1", "m1", **args)
        assert base != Cache.issues_key("h", "p1", "m1", schema_version=1, redacted=False)


class TestTaxonomy:
    def test_ids_are_stable_across_runs(self):
        assert cluster_id_for("Bash truncated at 30000 bytes") == cluster_id_for(
            "bash truncated at 30000 BYTES"
        )

    def test_similar_names_do_not_collide(self):
        assert cluster_id_for("Truncation") != cluster_id_for("truncation!")

    def test_first_run_marks_everything_new(self, tmp_path):
        from ossuary.agents.clusterer import ProposedCluster

        tax = Taxonomy(tmp_path / "taxonomy.json")
        clusters = tax.reconcile(
            [ProposedCluster(name="Empty tool results", summary="s", member_issue_ids=["i1"])],
            run_id="run-1",
            issue_lookup={"i1": "sess-1"},
        )
        assert len(clusters) == 1
        assert clusters[0].is_new_this_run
        assert clusters[0].affected_sessions == ["sess-1"]

    def test_second_run_recognises_a_known_cluster(self, tmp_path):
        from ossuary.agents.clusterer import ProposedCluster

        path = tmp_path / "taxonomy.json"
        tax = Taxonomy(path)
        first = tax.reconcile(
            [ProposedCluster(name="Empty tool results", summary="s", member_issue_ids=["i1"])],
            run_id="run-1", issue_lookup={"i1": "sess-1"},
        )
        tax.update(first, run_id="run-1")
        tax.save()

        again = Taxonomy(path)
        second = again.reconcile(
            [ProposedCluster(name="Empty tool results", summary="s2", member_issue_ids=["i2"])],
            run_id="run-2", issue_lookup={"i2": "sess-2"},
        )
        assert not second[0].is_new_this_run, "a known failure mode must not read as new"
        assert second[0].first_seen_run == "run-1"

    def test_model_can_claim_an_existing_id_under_a_new_name(self, tmp_path):
        from ossuary.agents.clusterer import ProposedCluster

        path = tmp_path / "taxonomy.json"
        tax = Taxonomy(path)
        first = tax.reconcile(
            [ProposedCluster(name="Original name", summary="s", member_issue_ids=[])],
            run_id="run-1", issue_lookup={},
        )
        tax.update(first, run_id="run-1")
        tax.save()
        known_id = first[0].cluster_id

        again = Taxonomy(path)
        second = again.reconcile(
            [ProposedCluster(name="Reworded name", summary="s",
                             member_issue_ids=[], existing_cluster_id=known_id)],
            run_id="run-2", issue_lookup={},
        )
        assert second[0].cluster_id == known_id
        assert not second[0].is_new_this_run

    def test_invented_issue_ids_are_discarded(self, tmp_path):
        from ossuary.agents.clusterer import ProposedCluster

        tax = Taxonomy(tmp_path / "taxonomy.json")
        clusters = tax.reconcile(
            [ProposedCluster(name="X", summary="s", member_issue_ids=["real", "hallucinated"])],
            run_id="run-1", issue_lookup={"real": "sess-1"},
        )
        assert clusters[0].member_issue_ids == ["real"]

    def test_corrupt_taxonomy_does_not_fail_the_run(self, tmp_path):
        path = tmp_path / "taxonomy.json"
        path.write_text("{ broken", encoding="utf-8")
        assert Taxonomy(path).known() == []

    def test_two_proposals_with_one_id_both_survive(self, tmp_path):
        from ossuary.agents.clusterer import ProposedCluster

        tax = Taxonomy(tmp_path / "taxonomy.json")
        clusters = tax.reconcile(
            [
                ProposedCluster(name="Same Name", summary="a", member_issue_ids=["i1"]),
                ProposedCluster(name="same name", summary="b", member_issue_ids=["i2"]),
            ],
            run_id="run-1", issue_lookup={"i1": "s1", "i2": "s2"},
        )
        assert len({c.cluster_id for c in clusters}) == 2, "a collision must not silently merge"

    def test_save_and_reload_preserves_history(self, tmp_path):
        path = tmp_path / "taxonomy.json"
        tax = Taxonomy(path)
        tax.update(
            [Cluster(cluster_id="c1", name="N", summary="S", first_seen_run="run-1")],
            run_id="run-1",
        )
        tax.save()
        tax2 = Taxonomy(path)
        tax2.update(
            [Cluster(cluster_id="c1", name="N", summary="S", first_seen_run="run-1")],
            run_id="run-2",
        )
        entry = tax2.entries["c1"]
        assert entry["total_runs_seen"] == 2
        assert entry["first_seen_run"] == "run-1"
        assert entry["last_seen_run"] == "run-2"
