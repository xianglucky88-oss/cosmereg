"""Transform hook: registration identity resolution.

The public entry points keep their original signatures so the collection
framework (`framework._apply_kernel`) and existing callers keep working:

* ``process(record)`` -> dict of derived fields merged into the record.
* ``reduce(records)`` -> the same records, annotated with merge results.

The merge logic lives in `cosmereg.identity`.  Instead of a bare
representative id, records now carry the confirmed cluster, the
still-undecided candidate relations, any conflicts, and the evidence for
every judgement.  ``reduce`` accepts an optional ``store`` (a
`cosmereg.store.Store`, or ``False`` to disable persistence); by default
it journals decisions to the configured database so confirmed identities
stay stable across recomputation and rule changes remain traceable.
"""
from .identity import (
    CONFIRMED,
    CANDIDATE,
    CONFLICT,
    RULE_VERSION,
    record_fields,
    record_override,
    relate,
    resolve,
    review_report,
    split_name,
)

__all__ = [
    "CONFIRMED",
    "CANDIDATE",
    "CONFLICT",
    "RULE_VERSION",
    "process",
    "reduce",
    "record_fields",
    "record_override",
    "relate",
    "resolve",
    "review_report",
    "split_name",
]


def process(record):
    return record_fields(record)


def reduce(records, store=None):
    return resolve(records, store=store)
