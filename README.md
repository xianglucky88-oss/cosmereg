# cosmereg

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make report    # print the identity review report (candidates, clusters, evidence)
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/cosmereg`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Identity resolution

Formula names that differ only by spaces, hyphens or a trailing suffix may
be spelling variants of one filing — or different filings altogether. The
merge step (`cosmereg.identity`, wired into the `cosmereg.kernel` hook)
therefore does not emit a bare representative id. Every record is annotated
with:

* `identity_state` — `confirmed`, `candidate`, `conflict` or `singleton`;
* `cluster_representative` / `cluster_size` / `identity_cluster` — the
  *confirmed* component only (candidates are never merged into it);
* `identity_candidates` — relations still awaiting a human decision, each
  with its evidence;
* `identity_evidence` — the evidence behind the confirmed membership.

Relation states and their rules:

* `confirmed` — same registration id with consistent attributes (spelling
  variants of the name are evidence, not obstacles), or a human override.
* `candidate` — name-level similarity across *different* registration ids
  (normalized-equal names or suffix variants). Never auto-confirmed: a
  similar name must not swallow a different filing.
* `conflict` — sources contradict each other (same registration id with
  different holders or unrelated names), or a human decision contradicts a
  rule. Always surfaced, never disguised as certain.
* `rejected` — a human decided the pair is not the same product.

Decisions are pure functions of record content and rule version and are
journaled in the `identity_decisions` table; human decisions live in
`identity_overrides`. Recomputing historical batches, changed page order,
duplicate content, late backfills and crash recovery therefore converge to
the same confirmed identities. Once a pair is confirmed it stays confirmed
across later rule versions; the new version's computation is still
journaled, so the impact of a rule change remains traceable in
`review_report()["rule_changes"]`.

Reviewer entry points:

```python
from cosmereg.identity import review_report, record_override

report = review_report()   # clusters, candidates, conflicts, rule_changes
record_override(None, "COS-1", "COS-2", "confirmed", note="reviewed")
```

`python -m cosmereg.cli --report` prints the same report as JSON. Record
payloads keep the annotation snapshot from their ingest run; the report is
the always-current view built from the journal.
