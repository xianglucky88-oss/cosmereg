"""Identity-resolution behaviour: candidates vs confirmed, evidence,
stability across recompute/order/duplicates/backfill/partial failure,
human decisions, and rule-change traceability."""
import json
import sqlite3

from cosmereg import identity, kernel
from cosmereg.config import SPEC
from cosmereg.identity import review_report
from cosmereg.pipeline import run
from cosmereg.store import Store


def mem_store():
    return Store(sqlite3.connect(":memory:"))


def rec(reg_id, name, holder="Acme", ts="2030-01-01T00:00:00Z", value=1, status="active"):
    return {"id": reg_id, "name": name, "value": value, "status": status,
            "ts": ts, "holder": holder}


def snapshot(records):
    return {
        r["record_key"]: (
            r["identity_state"],
            r["cluster_size"],
            r["cluster_representative"],
            r["identity_cluster"],
            json.dumps(r["identity_candidates"], sort_keys=True, ensure_ascii=False),
            json.dumps(r["identity_evidence"], sort_keys=True, ensure_ascii=False),
        )
        for r in records
    }


def rules(record_or_relation):
    return {e["rule"] for e in record_or_relation}


# --------------------------------------------------------------------------- #
# Name normalization.
# --------------------------------------------------------------------------- #
def test_name_splitting_recognizes_spelling_and_suffix_variants():
    assert identity.split_name("Alpha Widget")[0] == "alphawidget"
    assert identity.split_name("Alpha-Widget")[0] == "alphawidget"
    assert identity.split_name("alpha  widget")[0] == "alphawidget"
    key, stem, suffix = identity.split_name("Alpha Widget （清爽型）")
    assert stem == "alphawidget" and suffix == "清爽型"
    assert identity.split_name("Alpha Widget II")[1:] == ("alphawidget", "ii")
    assert identity.split_name("Alpha Widget")[2] == ""
    assert identity.split_name("")[0] == ""


def test_process_hook_keeps_its_contract():
    out = kernel.process({"id": "COS-1", "name": "Alpha Widget", "value": 7,
                          "status": "active", "ts": "t1", "holder": "Acme"})
    assert isinstance(out, dict)
    assert out["block_key"]
    assert out["name_key"] == "alphawidget"
    assert out["record_key"]
    assert out["rule_version"] == identity.RULE_VERSION


def test_record_key_ignores_derived_annotations():
    r = rec("COS-1", "Alpha Widget")
    before = identity.record_fields(r)["record_key"]
    kernel.reduce([r], store=False)
    assert identity.record_fields(r)["record_key"] == before


# --------------------------------------------------------------------------- #
# Confirmed vs candidate.
# --------------------------------------------------------------------------- #
def test_spelling_variants_of_one_registration_confirm():
    recs = [rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget"),
            rec("COS-1", "Alpha  Widget")]
    kernel.reduce(recs, store=False)
    assert {r["identity_state"] for r in recs} == {"confirmed"}
    assert {r["cluster_size"] for r in recs} == {3}
    assert {r["cluster_representative"] for r in recs} == {"COS-1"}
    assert len({r["identity_cluster"] for r in recs}) == 1
    roles = sorted(r["identity_role"] for r in recs)
    assert roles == ["member", "member", "representative"]
    for r in recs:
        assert "same_registration_id" in rules(r["identity_evidence"])
    assert any("name_spelling_variant" in rules(r["identity_evidence"]) for r in recs)


def test_similar_names_across_registrations_stay_candidates():
    recs = [rec("COS-1", "Alpha Widget"),
            rec("COS-2", "Alpha Widget"),    # same normalized name, other filing
            rec("COS-3", "Alpha-Widget"),    # spelling variant, other filing
            rec("COS-4", "Alpha Widget II"),  # suffix variant, other filing
            rec("COS-5", "Beta Gadget")]
    kernel.reduce(recs, store=False)
    by_id = {r["id"]: r for r in recs}
    for rid in ("COS-1", "COS-2", "COS-3", "COS-4"):
        r = by_id[rid]
        assert r["identity_state"] == "candidate"
        assert r["cluster_size"] == 1                      # nothing swallowed
        assert r["cluster_representative"] == rid
        assert r["identity_cluster"] == r["record_key"]
    assert by_id["COS-5"]["identity_state"] == "singleton"
    assert by_id["COS-5"]["identity_candidates"] == []
    related = {c["id"]: c for c in by_id["COS-1"]["identity_candidates"]}
    assert set(related) == {"COS-2", "COS-3", "COS-4"}
    assert all(c["state"] == "candidate" for c in related.values())
    assert "name_exact_normalized" in rules(related["COS-2"]["evidence"])
    assert "name_suffix_variant" in rules(related["COS-4"]["evidence"])
    assert all("different_registration_ids" in rules(c["evidence"])
               for c in related.values())


def test_conflicting_sources_are_not_disguised():
    recs = [rec("COS-1", "Alpha Widget", holder="Acme"),
            rec("COS-1", "Alpha Widget", holder="Beta Corp"),
            rec("COS-2", "Gamma Set"),
            rec("COS-2", "Delta Unit")]     # same id, unrelated name
    kernel.reduce(recs, store=False)
    assert {r["identity_state"] for r in recs} == {"conflict"}
    assert {r["cluster_size"] for r in recs} == {1}        # conflicts never merge
    first, second = recs[0]["identity_candidates"], recs[2]["identity_candidates"]
    assert first[0]["state"] == "conflict"
    assert "holder_conflict" in rules(first[0]["evidence"])
    assert "name_conflict" in rules(second[0]["evidence"])


# --------------------------------------------------------------------------- #
# Stability.
# --------------------------------------------------------------------------- #
def test_order_duplicates_and_recompute_are_stable():
    base = [rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget"),
            rec("COS-2", "Alpha Widget"), rec("COS-3", "Beta Gadget")]
    first = snapshot(kernel.reduce([dict(r) for r in base], store=False))
    shuffled = [dict(r) for r in reversed(base)] + [dict(base[0]), dict(base[2])]
    second = snapshot(kernel.reduce(shuffled, store=False))
    assert first == second


def test_confirmed_identity_survives_backfill_and_recompute():
    st = mem_store()
    try:
        batch1 = [rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget")]
        kernel.reduce(batch1, store=st)
        for r in batch1:
            st.upsert(r)
        cluster1 = {r["identity_cluster"] for r in batch1}
        assert len(cluster1) == 1

        # Later batch: same filings re-fetched plus a backfilled look-alike.
        batch2 = [rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget"),
                  rec("COS-2", "Alpha Widget")]
        kernel.reduce(batch2, store=st)
        confirmed = [r for r in batch2 if r["id"] == "COS-1"]
        assert {r["identity_cluster"] for r in confirmed} == cluster1
        assert all(r["identity_state"] == "confirmed" for r in confirmed)
        backfilled = [r for r in batch2 if r["id"] == "COS-2"][0]
        assert backfilled["identity_state"] == "candidate"
        assert backfilled["cluster_size"] == 1
        assert {c["id"] for c in backfilled["identity_candidates"]} == {"COS-1"}

        # Recomputation must not grow or alter the journal.
        decisions1 = st.decisions()
        kernel.reduce([rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget"),
                       rec("COS-2", "Alpha Widget")], store=st)
        decisions2 = st.decisions()
        assert len(decisions1) == len(decisions2)
        assert ({(d["pair_key"], d["rule_version"]): d["state"] for d in decisions1}
                == {(d["pair_key"], d["rule_version"]): d["state"] for d in decisions2})
    finally:
        st.close()


def test_legacy_records_without_identity_fields_are_resolved():
    st = mem_store()
    try:
        # Payload shape stored before the identity upgrade.
        legacy = {"id": "COS-1", "name": "Alpha Widget", "value": 1,
                  "status": "active", "ts": "2030-01-01T00:00:00Z", "holder": "Acme",
                  "block_key": "alphaw", "cluster_size": 3,
                  "cluster_representative": "COS-1"}
        st.upsert(legacy)
        batch = [rec("COS-1", "Alpha-Widget")]
        kernel.reduce(batch, store=st)
        assert batch[0]["identity_state"] == "confirmed"
        assert batch[0]["cluster_size"] == 2
        st.upsert(batch[0])
        report = review_report(store=st)
        assert len(report["clusters"]) == 1
        assert report["clusters"][0]["representative"] == "COS-1"
    finally:
        st.close()


def test_partial_failure_recovery_converges():
    st = mem_store()
    try:
        batch = [rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget"),
                 rec("COS-2", "Alpha Widget")]
        kernel.reduce(batch, store=st)
        st.upsert(batch[0])  # crash after persisting only part of the batch
        after_crash = {(d["pair_key"], d["rule_version"]): d["state"]
                       for d in st.decisions()}
        snap_crash = snapshot(batch)

        recovered = [rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget"),
                     rec("COS-2", "Alpha Widget")]
        kernel.reduce(recovered, store=st)
        after_recovery = {(d["pair_key"], d["rule_version"]): d["state"]
                          for d in st.decisions()}
        assert after_crash == after_recovery
        assert snap_crash == snapshot(recovered)
    finally:
        st.close()


# --------------------------------------------------------------------------- #
# Human decisions and rule changes.
# --------------------------------------------------------------------------- #
def test_human_decisions_are_respected():
    st = mem_store()
    try:
        a, b = rec("COS-1", "Alpha Widget"), rec("COS-2", "Alpha Widget")
        kernel.reduce([a, b], store=st)
        assert a["identity_state"] == "candidate"  # undecided, not certain

        identity.record_override(st, "COS-1", "COS-2", "confirmed", note="reviewed")
        kernel.reduce([a, b], store=st)
        assert a["identity_state"] == "confirmed"
        assert a["cluster_size"] == b["cluster_size"] == 2
        assert "human_override" in rules(a["identity_evidence"])

        identity.record_override(st, "COS-1", "COS-2", "rejected")
        kernel.reduce([a, b], store=st)
        assert a["identity_state"] == "singleton"
        assert a["cluster_size"] == 1
        assert a["identity_candidates"][0]["state"] == "rejected"

        # A human rejection contradicting a rule-confirmed pair is a conflict.
        c, d = rec("COS-9", "Gamma Set"), rec("COS-9", "Gamma  Set")
        kernel.reduce([c, d], store=st)
        assert c["identity_state"] == "confirmed"
        identity.record_override(st, "COS-9", "COS-9", "rejected")
        kernel.reduce([c, d], store=st)
        assert c["identity_state"] == d["identity_state"] == "conflict"
        assert "override_conflicts_rule" in rules(c["identity_candidates"][0]["evidence"])
    finally:
        st.close()


def test_rule_version_change_keeps_confirmation_and_stays_traceable():
    st = mem_store()
    try:
        a, b = rec("COS-1", "Alpha Widget"), rec("COS-2", "Alpha Widget")
        kernel.reduce([a, b], store=st)
        assert a["identity_state"] == "candidate"  # current rules do not confirm
        fa, fb = identity.record_fields(a), identity.record_fields(b)
        pk = identity.pair_key(fa, fb)
        # Simulate an older rule version that had confirmed this pair.
        st.record_decision({"pair_key": pk, "rule_version": "identity.v0",
                            "state": "confirmed", "reg_key": "reg|COS-1|COS-2",
                            "a_id": "COS-1", "b_id": "COS-2",
                            "a_key": fa["record_key"], "b_key": fb["record_key"],
                            "evidence": [{"rule": "legacy_rule", "detail": "v0"}]})
        kernel.reduce([a, b], store=st)
        assert a["identity_state"] == "confirmed"      # confirmation is stable
        assert a["cluster_size"] == b["cluster_size"] == 2
        assert b["cluster_representative"] == "COS-1"
        assert "rule_version_drift" in rules(a["identity_evidence"])

        report = review_report(store=st)
        changes = [c for c in report["rule_changes"] if c["pair_key"] == pk]
        assert len(changes) == 1
        assert changes[0]["versions"] == {"identity.v0": "confirmed",
                                          identity.RULE_VERSION: "candidate"}
    finally:
        st.close()


# --------------------------------------------------------------------------- #
# Auditor view and pipeline integration.
# --------------------------------------------------------------------------- #
def test_review_report_summarizes_states():
    st = mem_store()
    try:
        recs = [rec("COS-1", "Alpha Widget"), rec("COS-1", "Alpha-Widget"),
                rec("COS-2", "Alpha Widget"),
                rec("COS-3", "Beta Gadget", holder="Acme"),
                rec("COS-3", "Beta Gadget", holder="Beta Corp")]
        kernel.reduce(recs, store=st)
        for r in recs:
            st.upsert(r)
        report = review_report(store=st)
        assert report["rule_version"] == identity.RULE_VERSION
        assert len(report["clusters"]) == 1
        cluster = report["clusters"][0]
        assert cluster["representative"] == "COS-1" and cluster["size"] == 2
        assert cluster["evidence"]
        assert report["candidates"]          # COS-2 vs the COS-1 filings
        assert all(c["state"] == "candidate" for c in report["candidates"])
        assert all(c["evidence"] for c in report["candidates"])
        assert len(report["conflicts"]) == 1  # COS-3 holder conflict
        assert report["counts"]["candidates"] == len(report["candidates"])
        identity.record_override(st, "COS-1", "COS-2", "rejected", note="not same")
        report = review_report(store=st)
        assert report["overrides"] == [
            {"reg_key": "reg|COS-1|COS-2", "decision": "rejected", "note": "not same"}]
        assert report["counts"]["rejections"] >= 1
    finally:
        st.close()


def test_pipeline_journals_identity_decisions():
    con = Store().connect()
    st = Store(con)
    st.reset()
    try:
        result = run(con=con)
        assert result.get("engine") == SPEC.engine
        decisions = st.decisions()
        assert decisions  # the fixture contains same-name different-id rows
        report = review_report(store=st)
        assert report["counts"]["candidates"] >= 1
        # Similar names across filings must stay candidates, never merged.
        assert report["clusters"] == []
        assert all(c["state"] == "candidate" for c in report["candidates"])
    finally:
        st.reset()
        con.close()
