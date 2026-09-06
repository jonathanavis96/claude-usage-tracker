"""Which probe rows enter which series.

A probe row carries a `payload` tag: `prose` (the cache-write-heavy invariant
probe, the default when the key is absent, which is what every row before
2026-09-06 is) or `output` (the weekly Fable run whose job is to measure the
output class weight, see tracker/weight.py). An output row is a measurement of
the meter's weighting, not of the limit: its dollar value must never enter the
publisher's regime medians, change detection, the rotation's next-model choice
or the drift medians. Every consumer filters through here so the rule lives in
one place.

A prose row may also be flagged `"outlier": true` by the rotation's drift check
(tracker/rotate.py): its rerun agreed with the earlier median, so the reading is
kept in history but ignored. `usable_rows` is the set that may enter a median.
"""
from __future__ import annotations


def payload_of(row: dict) -> str:
    return row.get("payload") or "prose"


def is_output(row: dict) -> bool:
    return payload_of(row) == "output"


def prose_rows(rows: list[dict]) -> list[dict]:
    """The rows that measure the limit: everything that is not an output run."""
    return [r for r in rows if not is_output(r)]


def output_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if is_output(r)]


def is_outlier(row: dict) -> bool:
    return bool(row.get("outlier"))


def usable_rows(rows: list[dict]) -> list[dict]:
    """Prose rows that may enter a median or a published series: not flagged outliers."""
    return [r for r in prose_rows(rows) if not is_outlier(r)]
