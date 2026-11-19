"""Registration identity resolution for cosmetic filing records.

A formula name that differs only by spaces, hyphens or a trailing suffix
may be a spelling variant of one filing -- or a different filing
altogether.  This module therefore separates outcomes instead of emitting
a bare representative id:

* ``confirmed`` -- identity established by strong evidence (same
  registration id with consistent attributes, or a human override).
* ``candidate`` -- name-level similarity across *different* registration
  ids.  Candidates are never auto-merged; they wait for a human decision.
* ``conflict``  -- sources (or a human override) contradict each other.
  Conflicts are always surfaced, never disguised as certain conclusions.
* ``rejected``  -- a human decided the pair is not the same product.

Every judgement carries evidence (rule id + compared values) and is
persisted in the ``identity_decisions`` journal together with the rule
version.  Decisions are pure functions of record content and rule version,
so recomputing historical batches, changed page order, duplicate content,
late backfills and crash recovery all converge to the same confirmed
identities.  Once a pair is confirmed, later rule versions keep it
confirmed; the new version's computation is still journaled, so the impact
of rule changes stays traceable via ``review_report()["rule_changes"]``.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict

from .config import SPEC

RULE_VERSION = "identity.v1"

CONFIRMED = "confirmed"
CANDIDATE = "candidate"
CONFLICT = "conflict"
REJECTED = "rejected"
DISTINCT = "distinct"
SINGLETON = "singleton"

#: Record fields that define source content identity.
SOURCE_FIELDS = tuple(SPEC.columns)

_TOKEN_RE = re.compile(r"[0-9a-zA-Z一-鿿]+")  # alphanumerics and CJK
_PAREN_TAIL_RE = re.compile(r"[（(]([^()（）]*)[)）]\s*$")
_ROMAN_RE = re.compile(r"^[ivxl]+$")
_LATIN_SUFFIXES = {"plus", "pro", "max", "ultra", "mini", "lite"}
_CJK_SUFFIX_CHARS = ("型", "款", "版")

# Review urgency ordering for per-record relation lists.
_STATE_RANK = {CONFLICT: 0, CANDIDATE: 1, REJECTED: 2}


# --------------------------------------------------------------------------- #
# Name normalization.
# --------------------------------------------------------------------------- #
def split_name(name):
    """Split a formula name into ``(name_key, stem, suffix)``.

    ``name_key`` ignores spelling-only differences: letter case, width,
    spaces, hyphens and other separators.  ``stem``/``suffix`` peel off a
    trailing variant marker such as ``(滋润型)`` or ``II``; a shared stem
    is candidate evidence, never confirmation evidence.
    """
    text = unicodedata.normalize("NFKC", str(name or "")).casefold()
    suffix_parts = []
    while True:
        m = _PAREN_TAIL_RE.search(text)
        if not m:
            break
        if m.group(1).strip():
            suffix_parts.append(m.group(1).strip())
        text = text[:m.start()]
    tokens = _TOKEN_RE.findall(text)
    name_key = "".join(tokens)
    if len(tokens) > 1:
        last = tokens[-1]
        if (last.isdigit() or _ROMAN_RE.match(last) or last in _LATIN_SUFFIXES
                or (len(last) >= 2 and last.endswith(_CJK_SUFFIX_CHARS))):
            suffix_parts.insert(0, last)
            tokens = tokens[:-1]
    return name_key, "".join(tokens), " ".join(suffix_parts)


def normalize_holder(holder):
    text = unicodedata.normalize("NFKC", str(holder or "")).casefold()
    return " ".join(text.split())


def record_fields(record):
    """Derived identity fields for one record (pure, deterministic)."""
    name_key, stem, suffix = split_name(record.get("name"))
    src = {c: record.get(c) for c in SOURCE_FIELDS if c in record}
    blob = json.dumps(src, sort_keys=True, ensure_ascii=False)
    return {
        "record_key": hashlib.sha256(blob.encode()).hexdigest()[:24],
        "reg_id": str(record.get("id") or ""),
        "name_key": name_key,
        "name_stem": stem,
        "name_suffix": suffix,
        "holder_key": normalize_holder(record.get("holder")),
        "block_key": (stem or name_key)[:12] or "x",
        "rule_version": RULE_VERSION,
    }


# --------------------------------------------------------------------------- #
# Pairwise relations.
# --------------------------------------------------------------------------- #
def _ev(rule, detail="", **extra):
    d = {"rule": rule, "detail": detail}
    d.update(extra)
    return d


def relate(a, b):
    """Pure pairwise relation between two records.

    Returns ``{"state": ..., "evidence": [...]}``.  Different registration
    ids are never auto-confirmed: name similarity alone yields at most a
    ``candidate`` that waits for a human decision.
    """
    fa, fb = record_fields(a), record_fields(b)
    ida, idb = fa["reg_id"], fb["reg_id"]
    ka, kb = fa["name_key"], fb["name_key"]
    ha, hb = fa["holder_key"], fb["holder_key"]
    holder_known = bool(ha) and bool(hb)
    if ida and ida == idb:
        ev = [_ev("same_registration_id", ida)]
        if holder_known and ha != hb:
            ev.append(_ev("holder_conflict", "%s vs %s" % (a.get("holder"), b.get("holder"))))
            return {"state": CONFLICT, "evidence": ev}
        if ka == kb or not ka or not kb:
            if ka and str(a.get("name") or "") != str(b.get("name") or ""):
                ev.append(_ev("name_spelling_variant", "%r vs %r" % (a.get("name"), b.get("name"))))
            if not ka or not kb:
                ev.append(_ev("name_unverified", "%r vs %r" % (a.get("name"), b.get("name"))))
            ev.append(_ev("holder_match" if holder_known else "holder_unverified", ha or hb))
            return {"state": CONFIRMED, "evidence": ev}
        if fa["name_stem"] and fa["name_stem"] == fb["name_stem"]:
            ev.append(_ev("name_suffix_variant",
                          "%s vs %s" % (fa["name_suffix"] or "-", fb["name_suffix"] or "-")))
            return {"state": CONFIRMED, "evidence": ev}
        ev.append(_ev("name_conflict", "%r vs %r" % (a.get("name"), b.get("name"))))
        return {"state": CONFLICT, "evidence": ev}
    if not ka or not kb:
        return {"state": DISTINCT, "evidence": []}
    if ka == kb:
        ev = [_ev("name_exact_normalized", ka)]
    elif fa["name_stem"] and fa["name_stem"] == fb["name_stem"]:
        ev = [_ev("name_suffix_variant",
                  "%s vs %s" % (fa["name_suffix"] or "-", fb["name_suffix"] or "-"))]
    else:
        return {"state": DISTINCT, "evidence": []}
    if holder_known:
        ev.append(_ev("holder_match" if ha == hb else "holder_mismatch",
                      "%s vs %s" % (a.get("holder"), b.get("holder"))))
    else:
        ev.append(_ev("holder_unverified", ha or hb))
    ev.append(_ev("different_registration_ids", "%s vs %s" % (ida or "?", idb or "?")))
    return {"state": CANDIDATE, "evidence": ev}


def pair_key(fa, fb):
    """Observation-level pair identity: stable while both contents are."""
    a, b = sorted([fa["record_key"], fb["record_key"]])
    return "obs|%s|%s" % (a, b)


def reg_key(fa, fb):
    """Registration-level relation key used for human overrides."""
    if fa["reg_id"] and fb["reg_id"]:
        return "reg|" + "|".join(sorted({fa["reg_id"], fb["reg_id"]}))
    return pair_key(fa, fb)


def reg_key_for_ids(id_a, id_b):
    ids = sorted({str(id_a or ""), str(id_b or "")} - {""})
    if not ids:
        raise ValueError("at least one registration id is required")
    return "reg|" + "|".join(ids)


# --------------------------------------------------------------------------- #
# Decision resolution.
# --------------------------------------------------------------------------- #
def _effective_state(rows, override):
    """Resolve the effective state of one pair.

    ``rows`` are the journaled rule decisions (all versions, evidence
    already parsed); ``override`` is the human decision, if any.  A human
    decision always wins; a source conflict always surfaces; an earlier
    confirmation stays confirmed across rule versions, with drift noted.
    """
    current = next((r for r in rows if r["rule_version"] == RULE_VERSION), None)
    if override:
        note = override.get("note", "")
        if override["decision"] == CONFIRMED:
            ev = [_ev("human_override", note, decision=CONFIRMED)]
            if current and current["state"] == CONFLICT:
                ev += current["evidence"]  # keep the contradiction visible
            return CONFIRMED, ev
        if override["decision"] == REJECTED:
            if current and current["state"] == CONFIRMED:
                return CONFLICT, [_ev("override_conflicts_rule",
                                      "human rejected but %s computes confirmed" % RULE_VERSION)]
            return REJECTED, [_ev("human_override", note, decision=REJECTED)]
    if current and current["state"] == CONFLICT:
        return CONFLICT, list(current["evidence"])
    confirmed_row = next((r for r in rows if r["state"] == CONFIRMED), None)
    if confirmed_row:
        ev = list(confirmed_row["evidence"])
        if current and current["state"] != CONFIRMED:
            ev.append(_ev("rule_version_drift",
                          "%s computes %s; earlier confirmation kept"
                          % (RULE_VERSION, current["state"])))
        return CONFIRMED, ev
    if current:
        return current["state"], list(current["evidence"])
    return DISTINCT, []


class _Dsu:
    """Union-find with deterministic (smallest-key) roots."""

    def __init__(self):
        self.parent = {}

    def find(self, x):
        p = self.parent
        p.setdefault(x, x)
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _open_default_store():
    try:
        from .store import Store
        store = Store()
        store.connect()
        return store
    except Exception:
        return None


def _resolve_store(store):
    """Return ``(store, owned)``; ``store=False`` disables persistence."""
    if store is False or store is not None:
        return (None if store is False else store), False
    default = _open_default_store()
    return default, default is not None


def resolve(records, store=None):
    """Annotate ``records`` with identity results and return them.

    The identity universe is ``records`` plus everything already in the
    store, so backfilled or re-fetched rows resolve against full history.
    Results are deterministic: input order, duplicate content and
    recomputation do not change them.  Decisions are journaled with the
    current rule version; the journal is the audit trail.
    """
    records = list(records)
    st, owned = _resolve_store(store)
    try:
        return _resolve_with_store(records, st)
    finally:
        if owned:
            st.close()


def _resolve_with_store(records, st):
    stored = st.all_records() if st else []
    prior_rows = st.decisions() if st else []
    overrides = st.overrides() if st else {}
    prior = defaultdict(list)
    for row in prior_rows:
        prior[row["pair_key"]].append(row)

    urec, ufield = {}, {}
    for r in list(stored) + records:
        f = record_fields(r)
        rk = f["record_key"]
        urec.setdefault(rk, r)
        ufield[rk] = f

    # Candidate-generation buckets: only same-id or same-stem records can
    # have a non-distinct relation, so pairs are enumerated within buckets.
    buckets = defaultdict(list)
    for rk in sorted(ufield, key=lambda k: (ufield[k]["reg_id"], k)):
        f = ufield[rk]
        if f["reg_id"]:
            buckets["id|" + f["reg_id"]].append(rk)
        if f["name_stem"]:
            buckets["stem|" + f["name_stem"]].append(rk)
    pairs = set()
    for members in buckets.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))

    edges = {}
    journal = []
    for rka, rkb in sorted(pairs):
        fa, fb = ufield[rka], ufield[rkb]
        rel = relate(urec[rka], urec[rkb])
        if rel["state"] == DISTINCT:
            continue
        pk, rk_ = pair_key(fa, fb), reg_key(fa, fb)
        row = {"pair_key": pk, "reg_key": rk_, "rule_version": RULE_VERSION,
               "state": rel["state"], "a_id": fa["reg_id"], "b_id": fb["reg_id"],
               "a_key": rka, "b_key": rkb, "evidence": rel["evidence"]}
        journal.append(row)
        rows = [r for r in prior.get(pk, []) if r["rule_version"] != RULE_VERSION]
        rows.append(row)
        state, evidence = _effective_state(rows, overrides.get(rk_))
        edges[pk] = {"state": state, "evidence": evidence, "fa": fa, "fb": fb}

    if st:
        for row in journal:
            st.record_decision(row)

    dsu = _Dsu()
    for e in edges.values():
        if e["state"] == CONFIRMED:
            dsu.union(e["fa"]["record_key"], e["fb"]["record_key"])
    members = defaultdict(list)
    for rk in ufield:
        members[dsu.find(rk)].append(rk)
    rep_of = {root: min(rks, key=lambda k: (ufield[k]["reg_id"], k))
              for root, rks in members.items()}
    edges_of = defaultdict(list)
    for e in edges.values():
        edges_of[e["fa"]["record_key"]].append(e)
        edges_of[e["fb"]["record_key"]].append(e)

    for r in records:
        f = record_fields(r)
        rk = f["record_key"]
        comp = members[dsu.find(rk)]
        rep = rep_of[dsu.find(rk)]
        confirmed_ev, seen, relations = [], set(), []
        has_conflict = has_candidate = False
        for e in edges_of.get(rk, []):
            other = e["fb"] if e["fa"]["record_key"] == rk else e["fa"]
            if e["state"] == CONFIRMED:
                for item in e["evidence"]:
                    sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if sig not in seen:
                        seen.add(sig)
                        confirmed_ev.append(item)
            else:
                has_conflict = has_conflict or e["state"] == CONFLICT
                has_candidate = has_candidate or e["state"] == CANDIDATE
                relations.append({"id": other["reg_id"], "record_key": other["record_key"],
                                  "state": e["state"], "evidence": e["evidence"]})
        relations.sort(key=lambda x: (_STATE_RANK.get(x["state"], 9),
                                      x["id"], x["record_key"]))
        size = len(comp)
        if has_conflict:
            state = CONFLICT
        elif size > 1:
            state = CONFIRMED
        elif has_candidate:
            state = CANDIDATE
        else:
            state = SINGLETON
        r.update({
            "block_key": f["block_key"],
            "record_key": rk,
            "name_key": f["name_key"],
            "name_stem": f["name_stem"],
            "name_suffix": f["name_suffix"],
            "rule_version": RULE_VERSION,
            "identity_state": state,
            "identity_cluster": rep if size > 1 else rk,
            "identity_role": ("representative" if rep == rk else "member") if size > 1 else SINGLETON,
            "identity_evidence": confirmed_ev,
            "identity_candidates": relations,
            "cluster_representative": ufield[rep]["reg_id"] if size > 1 else f["reg_id"],
            "cluster_size": size,
        })
    return records


# --------------------------------------------------------------------------- #
# Human decisions.
# --------------------------------------------------------------------------- #
def record_override(store, id_a, id_b, decision, note=""):
    """Record a human decision for a registration-level relation.

    ``decision`` is ``confirmed`` or ``rejected``; either way the pair
    stops being an undecided candidate.  Returns the relation key.
    """
    if decision not in (CONFIRMED, REJECTED):
        raise ValueError("decision must be %r or %r" % (CONFIRMED, REJECTED))
    st, owned = _resolve_store(store)
    if st is None:
        raise RuntimeError("identity store is unavailable")
    key = reg_key_for_ids(id_a, id_b)
    try:
        st.record_override(key, decision, note)
    finally:
        if owned:
            st.close()
    return key


# --------------------------------------------------------------------------- #
# Auditor view.
# --------------------------------------------------------------------------- #
def review_report(store=None):
    """Build the reviewer view from the store: confirmed clusters,
    undecided candidates, conflicts, rejections and rule-version drift."""
    st, owned = _resolve_store(store)
    if st is None:
        raise RuntimeError("identity store is unavailable")
    try:
        stored = st.all_records()
        decisions = st.decisions()
        overrides = st.overrides()
    finally:
        if owned:
            st.close()

    ufield = {}
    for r in stored:
        f = record_fields(r)
        ufield[f["record_key"]] = f

    by_pair = defaultdict(list)
    for row in decisions:
        by_pair[row["pair_key"]].append(row)

    pair_views = []
    dsu = _Dsu()
    for pk in sorted(by_pair):
        rows = by_pair[pk]
        first = rows[0]
        state, evidence = _effective_state(rows, overrides.get(first["reg_key"]))
        view = {"pair_key": pk, "reg_key": first["reg_key"],
                "pair": sorted({first["a_id"], first["b_id"]} - {""}),
                "state": state, "evidence": evidence,
                "a_key": first["a_key"], "b_key": first["b_key"],
                "versions": {r["rule_version"]: r["state"] for r in rows}}
        pair_views.append(view)
        if state == CONFIRMED and first["a_key"] in ufield and first["b_key"] in ufield:
            dsu.union(first["a_key"], first["b_key"])

    members = defaultdict(list)
    for rk in ufield:
        members[dsu.find(rk)].append(rk)
    clusters = []
    for rks in members.values():
        if len(rks) < 2:
            continue
        comp = set(rks)
        ordered = sorted(rks, key=lambda k: (ufield[k]["reg_id"], k))
        evidence, seen = [], set()
        for v in pair_views:
            if v["state"] == CONFIRMED and v["a_key"] in comp and v["b_key"] in comp:
                for item in v["evidence"]:
                    sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if sig not in seen:
                        seen.add(sig)
                        evidence.append(item)
        clusters.append({"representative": ufield[ordered[0]]["reg_id"],
                         "cluster": ordered[0],
                         "members": [ufield[k]["reg_id"] for k in ordered],
                         "size": len(ordered), "evidence": evidence})
    clusters.sort(key=lambda c: (c["representative"], c["cluster"]))

    def _views(state):
        return [{k: v[k] for k in ("pair_key", "reg_key", "pair", "state",
                                   "evidence", "versions")}
                for v in pair_views if v["state"] == state]

    rule_changes = [{"pair_key": v["pair_key"], "reg_key": v["reg_key"],
                     "pair": v["pair"], "versions": v["versions"]}
                    for v in pair_views if len(set(v["versions"].values())) > 1]
    human = [{"reg_key": key, "decision": o["decision"], "note": o.get("note", "")}
             for key, o in sorted(overrides.items())]
    return {
        "rule_version": RULE_VERSION,
        "clusters": clusters,
        "candidates": _views(CANDIDATE),
        "conflicts": _views(CONFLICT),
        "rejections": _views(REJECTED),
        "rule_changes": rule_changes,
        "overrides": human,
        "counts": {"records": len(ufield), "clusters": len(clusters),
                   "candidates": len([v for v in pair_views if v["state"] == CANDIDATE]),
                   "conflicts": len([v for v in pair_views if v["state"] == CONFLICT]),
                   "rejections": len([v for v in pair_views if v["state"] == REJECTED]),
                   "decisions": len(decisions)},
    }
