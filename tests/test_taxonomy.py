"""Cluster identity: stable ids, and what the taxonomy will and will not accept."""

from __future__ import annotations


from ossuary.models import Cluster, ProposedCluster
from ossuary.taxonomy import Taxonomy, cluster_id_for


class TestTaxonomy:
    def test_ids_are_stable_across_runs(self):
        assert cluster_id_for("Bash truncated at 30000 bytes") == cluster_id_for(
            "bash truncated at 30000 BYTES"
        )

    def test_similar_names_do_not_collide(self):
        assert cluster_id_for("Truncation") != cluster_id_for("truncation!")

    def test_first_run_marks_everything_new(self, tmp_path):
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
