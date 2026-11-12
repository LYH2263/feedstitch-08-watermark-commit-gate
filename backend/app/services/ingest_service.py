from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.merge_rules import decide_merge, eligible_for_incremental
from app.engines.parse_feed import parse_fixture
from app.modules import watermark_commit_gate
from app.repositories import canonical as canon_repo
from app.repositories import runs as runs_repo
from app.repositories import settings as settings_repo
from app.repositories import sources as sources_repo


def preview_source(source_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        source = sources_repo.get_source(conn, source_id)
        if not source:
            raise ValueError("source not found")
        path = FIXTURES_DIR / source["fixture_path"]
        items = parse_fixture(path, source["format"])
        return {
            "source": source,
            "count": len(items),
            "items": items[:50],
        }
    finally:
        conn.close()


def run_ingest(source_id: int, mode: str = "incremental") -> dict[str, Any]:
    if mode not in ("incremental", "full"):
        raise ValueError("mode must be incremental or full")

    conn = connect()
    try:
        source = sources_repo.get_source(conn, source_id)
        if not source:
            raise ValueError("source not found")
        if not source["enabled"]:
            raise ValueError("source disabled")

        # Snapshot pre-run state. The watermark is only ever written AFTER the
        # commit gate passes; a full reload ignores the stored watermark for
        # eligibility but must not clear it up front.
        pre_run_watermark = source["watermark_ts"]
        pre_run_canonical_count = canon_repo.count_items(conn)
        eligibility_watermark = None if mode == "full" else pre_run_watermark

        run_id = runs_repo.create_run(conn, source_id, mode)
        path = FIXTURES_DIR / source["fixture_path"]

        try:
            items = parse_fixture(path, source["format"])
        except Exception as exc:  # noqa: BLE001
            runs_repo.finish_run(
                conn,
                run_id,
                status="failed",
                parsed=0,
                upserted=0,
                conflicts=0,
                skipped=0,
                report={"error": str(exc)},
            )
            conn.commit()
            raise

        parsed = 0
        upserted = 0
        conflicts = 0
        skipped = 0
        max_event_ts: str | None = eligibility_watermark
        dispositions: list[str] = []

        for item in items:
            parsed += 1
            if mode == "incremental" and not eligible_for_incremental(
                item["event_ts"],
                eligibility_watermark,
                int(source.get("lookback_seconds") or 0),
            ):
                runs_repo.insert_staging(
                    conn,
                    run_id=run_id,
                    source_id=source_id,
                    item=item,
                    disposition="skipped_watermark",
                )
                skipped += 1
                dispositions.append("skipped_watermark")
                continue

            existing = canon_repo.get_by_key(conn, item["business_key"])
            existing_view = None
            if existing:
                existing_view = {
                    "business_key": existing["business_key"],
                    "title": existing["title"],
                    "price": existing["price"],
                    "category": existing["category"],
                    "event_ts": existing["event_ts"],
                    "source_revision": existing["source_revision"],
                    "payload": existing["payload"],
                }

            decision = decide_merge(existing_view, item)
            if decision in ("insert", "upsert"):
                canon_repo.upsert_item(conn, item, source_id)
                runs_repo.insert_staging(
                    conn,
                    run_id=run_id,
                    source_id=source_id,
                    item=item,
                    disposition=decision,
                )
                upserted += 1
                dispositions.append(decision)
                if max_event_ts is None or item["event_ts"] > max_event_ts:
                    max_event_ts = item["event_ts"]
            elif decision == "conflict":
                runs_repo.insert_conflict(
                    conn,
                    run_id=run_id,
                    source_id=source_id,
                    business_key=item["business_key"],
                    existing_payload=existing_view["payload"] if existing_view else {},
                    incoming_payload=item["payload"],
                    reason="non_key_field_mismatch",
                )
                runs_repo.insert_staging(
                    conn,
                    run_id=run_id,
                    source_id=source_id,
                    item=item,
                    disposition="quarantine",
                )
                conflicts += 1
                dispositions.append("quarantine")
                if max_event_ts is None or item["event_ts"] > max_event_ts:
                    max_event_ts = item["event_ts"]
            else:
                runs_repo.insert_staging(
                    conn,
                    run_id=run_id,
                    source_id=source_id,
                    item=item,
                    disposition="skip_stale",
                )
                skipped += 1
                dispositions.append("skip_stale")
                if max_event_ts is None or item["event_ts"] > max_event_ts:
                    max_event_ts = item["event_ts"]

        counters = {
            "parsed": parsed,
            "upserted": upserted,
            "conflicts": conflicts,
            "skipped": skipped,
        }
        # Controlled fault injection (demo/chaos hook, settings-driven). Runs
        # after processing and before the gate, simulating a pipeline fault
        # that the commit gate must catch. 'off' in normal operation.
        fault = settings_repo.get_all(conn).get(watermark_commit_gate.FAULT_SETTING_KEY, "off")
        fault_note = watermark_commit_gate.inject_fault(
            conn, fault=fault, run_id=run_id, counters=counters
        )

        candidate_watermark = max_event_ts
        post_run_canonical_count = canon_repo.count_items(conn)

        # Commit gate: reconcile this run's report against the persisted
        # staging / conflicts / canonical tables BEFORE touching the watermark.
        gate = watermark_commit_gate.evaluate(
            conn,
            run_id=run_id,
            mode=mode,
            parsed=counters["parsed"],
            upserted=counters["upserted"],
            conflicts=counters["conflicts"],
            skipped=counters["skipped"],
            pre_run_watermark_ts=pre_run_watermark,
            candidate_watermark_ts=candidate_watermark,
            pre_run_canonical_count=pre_run_canonical_count,
            post_run_canonical_count=post_run_canonical_count,
        )

        if gate.passed:
            if candidate_watermark:
                sources_repo.set_watermark(conn, source_id, candidate_watermark)
            committed_watermark = candidate_watermark if candidate_watermark else pre_run_watermark
            status = "ok"
        else:
            # Gate failed: keep the pre-run watermark untouched so later
            # increments are not based on an unreconciled position.
            committed_watermark = pre_run_watermark
            status = "gate_failed"

        report = {
            "source_id": source_id,
            "mode": mode,
            "parsed_count": counters["parsed"],
            "upserted_count": counters["upserted"],
            "conflict_count": counters["conflicts"],
            "skipped_count": counters["skipped"],
            "pre_run_canonical_count": pre_run_canonical_count,
            "canonical_count": post_run_canonical_count,
            "pre_run_watermark_ts": pre_run_watermark,
            "candidate_watermark_ts": candidate_watermark,
            "committed_watermark_ts": committed_watermark,
            "watermark_committed": bool(gate.passed and candidate_watermark),
            "dispositions": dispositions,
            "gate": gate.as_dict(),
            "fault_injection": fault_note,
        }
        runs_repo.finish_run(
            conn,
            run_id,
            status=status,
            parsed=counters["parsed"],
            upserted=counters["upserted"],
            conflicts=counters["conflicts"],
            skipped=counters["skipped"],
            report=report,
        )
        conn.commit()
        run = runs_repo.get_run(conn, run_id)
        return run or report
    finally:
        conn.close()


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        sources = sources_repo.list_sources(conn)
        return {
            "source_count": len(sources),
            "enabled_sources": sum(1 for s in sources if s["enabled"]),
            "canonical_count": canon_repo.count_items(conn),
            "conflict_count": conn.execute("SELECT COUNT(*) c FROM conflicts").fetchone()["c"],
            "run_count": conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"],
            "staging_count": conn.execute("SELECT COUNT(*) c FROM staging_rows").fetchone()["c"],
        }
    finally:
        conn.close()
