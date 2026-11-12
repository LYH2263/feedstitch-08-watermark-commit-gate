"""Watermark commit gate (水位提交闸).

Runs after a run has finished processing (staging / canonical / conflicts) and
BEFORE the source watermark is advanced. The gate independently recomputes the
run's numbers from the persisted tables and reconciles them against what the
run report claims:

- staging row count vs parsed_count
- staging disposition counts vs upserted / conflict / skipped counts
- conflict rows written for the run vs conflict_count
- canonical row-count delta vs the number of ``insert`` dispositions
- candidate watermark monotonicity (incremental runs never move it backwards)

Only when every check passes may the caller commit the candidate watermark.
On failure the pre-run watermark must be kept and the run is marked
``gate_failed`` so "processed" stays distinguishable from "reconciled +
watermark committed".

The module also provides a small settings-driven fault injector
(``inject_fault``) so the failure path can be demonstrated on local fixtures:
setting ``gate_fault_injection`` = off | drop_staging_row |
skew_report_counter | canonical_drift.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

UPSERT_DISPOSITIONS = ("insert", "upsert")
SKIP_DISPOSITIONS = ("skipped_watermark", "skip_stale")
QUARANTINE_DISPOSITION = "quarantine"

FAULT_SETTING_KEY = "gate_fault_injection"
FAULT_KINDS = ("off", "drop_staging_row", "skew_report_counter", "canonical_drift")


@dataclass
class GateCheck:
    name: str
    ok: bool
    expected: Any
    actual: Any
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass
class GateResult:
    passed: bool
    checks: list[GateCheck]
    pre_run_watermark_ts: str | None
    candidate_watermark_ts: str | None

    @property
    def failed_checks(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        if self.passed:
            return f"all {len(self.checks)} reconcile checks passed"
        names = ", ".join(c.name for c in self.failed_checks)
        return f"{len(self.failed_checks)}/{len(self.checks)} checks failed: {names}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary(),
            "pre_run_watermark_ts": self.pre_run_watermark_ts,
            "candidate_watermark_ts": self.candidate_watermark_ts,
            "checks": [c.as_dict() for c in self.checks],
        }


def _staging_disposition_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT disposition, COUNT(*) c FROM staging_rows WHERE run_id = ? GROUP BY disposition",
        (run_id,),
    ).fetchall()
    return {r["disposition"]: int(r["c"]) for r in rows}


def evaluate(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    mode: str,
    parsed: int,
    upserted: int,
    conflicts: int,
    skipped: int,
    pre_run_watermark_ts: str | None,
    candidate_watermark_ts: str | None,
    pre_run_canonical_count: int,
    post_run_canonical_count: int,
) -> GateResult:
    """Reconcile the run's claimed counts against the persisted tables."""
    checks: list[GateCheck] = []
    staging = _staging_disposition_counts(conn, run_id)

    def add(name: str, ok: bool, expected: Any, actual: Any, detail: str) -> None:
        checks.append(GateCheck(name=name, ok=ok, expected=expected, actual=actual, detail=detail))

    staging_total = sum(staging.values())
    add(
        "staging_total_matches_parsed",
        staging_total == parsed,
        parsed,
        staging_total,
        "staging rows persisted for this run must equal parsed_count",
    )

    staged_upserts = sum(staging.get(d, 0) for d in UPSERT_DISPOSITIONS)
    add(
        "staging_upserts_match_report",
        staged_upserts == upserted,
        upserted,
        staged_upserts,
        "staging rows with disposition insert/upsert must equal upserted_count",
    )

    staged_quarantine = staging.get(QUARANTINE_DISPOSITION, 0)
    add(
        "staging_quarantine_matches_conflicts",
        staged_quarantine == conflicts,
        conflicts,
        staged_quarantine,
        "staging rows quarantined must equal conflict_count",
    )

    staged_skips = sum(staging.get(d, 0) for d in SKIP_DISPOSITIONS)
    add(
        "staging_skips_match_report",
        staged_skips == skipped,
        skipped,
        staged_skips,
        "staging rows skipped (watermark/stale) must equal skipped_count",
    )

    conflict_rows = int(
        conn.execute("SELECT COUNT(*) c FROM conflicts WHERE run_id = ?", (run_id,)).fetchone()["c"]
    )
    add(
        "conflict_rows_match_report",
        conflict_rows == conflicts,
        conflicts,
        conflict_rows,
        "conflict rows persisted for this run must equal conflict_count",
    )

    staged_inserts = staging.get("insert", 0)
    canonical_delta = post_run_canonical_count - pre_run_canonical_count
    add(
        "canonical_delta_matches_inserts",
        canonical_delta == staged_inserts,
        staged_inserts,
        canonical_delta,
        "canonical row-count change must equal the number of insert dispositions "
        "(upsert/conflict/skip do not change the row count)",
    )

    if mode == "incremental" and pre_run_watermark_ts and candidate_watermark_ts:
        add(
            "watermark_monotonic",
            candidate_watermark_ts >= pre_run_watermark_ts,
            f">= {pre_run_watermark_ts}",
            candidate_watermark_ts,
            "incremental runs must never move the watermark backwards",
        )

    return GateResult(
        passed=all(c.ok for c in checks),
        checks=checks,
        pre_run_watermark_ts=pre_run_watermark_ts,
        candidate_watermark_ts=candidate_watermark_ts,
    )


def inject_fault(
    conn: sqlite3.Connection,
    *,
    fault: str | None,
    run_id: int,
    counters: dict[str, int],
) -> str | None:
    """Controlled inconsistency injection, for demonstrating the failure path.

    Simulates a pipeline fault AFTER processing but BEFORE the gate runs; the
    gate must catch the resulting mismatch and refuse the watermark commit.
    ``counters`` holds the in-memory run counters and may be mutated.
    Returns a human-readable note for the run report, or None when off.
    """
    if not fault or fault == "off":
        return None
    if fault == "drop_staging_row":
        row = conn.execute(
            "SELECT id, business_key FROM staging_rows WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return "drop_staging_row: no staging row to delete (no-op)"
        conn.execute("DELETE FROM staging_rows WHERE id = ?", (row["id"],))
        return f"drop_staging_row: deleted staging row {row['id']} ({row['business_key']}) of run {run_id}"
    if fault == "skew_report_counter":
        counters["upserted"] += 1
        return "skew_report_counter: upserted counter +1 without a matching write"
    if fault == "canonical_drift":
        key = f"GATE-PHANTOM-{run_id}"
        conn.execute(
            """
            INSERT OR IGNORE INTO canonical_items(
                business_key, title, price, category, source_id,
                event_ts, source_revision, payload_json, updated_at
            ) VALUES (?,?,?,?,0,?,?,?,datetime('now'))
            """,
            (key, "Gate Phantom", 0.0, "phantom", "1970-01-01T00:00:00Z", 0, "{}"),
        )
        return f"canonical_drift: inserted phantom canonical row {key} outside the write path"
    return f"unknown fault {fault!r}: ignored"


def watermark_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-source watermark view, same-source with the runs table.

    For each source: current watermark, the last successful commit (latest run
    with status 'ok'), the latest gate outcome, and whether the displayed
    watermark equals the last committed one.
    """
    sources = conn.execute(
        "SELECT id, name, watermark_ts, lookback_seconds FROM sources ORDER BY id"
    ).fetchall()
    last_ok: dict[int, sqlite3.Row] = {}
    last_any: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT source_id, id, status, report_json, created_at FROM runs ORDER BY id DESC"
    ).fetchall():
        last_any.setdefault(r["source_id"], r)
        if r["status"] == "ok":
            last_ok.setdefault(r["source_id"], r)

    out: list[dict[str, Any]] = []
    for s in sources:
        sid = s["id"]
        last_commit = None
        ok_run = last_ok.get(sid)
        if ok_run is not None:
            report = json.loads(ok_run["report_json"] or "{}")
            last_commit = {
                "run_id": ok_run["id"],
                "committed_watermark_ts": report.get("committed_watermark_ts"),
                "created_at": ok_run["created_at"],
            }
        last_gate = None
        any_run = last_any.get(sid)
        if any_run is not None:
            report = json.loads(any_run["report_json"] or "{}")
            gate = report.get("gate") or {}
            last_gate = {
                "run_id": any_run["id"],
                "status": any_run["status"],
                "passed": gate.get("passed"),
                "summary": gate.get("summary"),
                "created_at": any_run["created_at"],
            }
        committed = last_commit["committed_watermark_ts"] if last_commit else None
        out.append(
            {
                "source_id": sid,
                "name": s["name"],
                "watermark_ts": s["watermark_ts"],
                "lookback_seconds": s["lookback_seconds"],
                "last_commit": last_commit,
                "last_gate": last_gate,
                "consistent": s["watermark_ts"] == committed,
            }
        )
    return out
