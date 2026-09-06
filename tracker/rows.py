"""Which probe rows enter which series.

A probe row carries a `payload` tag: `prose` (the cache-write-heavy invariant
probe, the default when the key is absent, which is what every row before
2026-09-06 is) or `output` (the weekly Fable run whose job is to measure the
output class weight, see tracker/weight.py). An output row is a measurement of
the meter's weighting, not of the limit: its dollar value must never enter the
publisher's regime medians, change detection, the rotation's next-model choice
or the drift medians. Every consumer filters through here so the rule lives in
one place.
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
