"""Service-level tests for the watermark commit gate.

Each test runs against a fresh SQLite DB (tmp_path) seeded with the standard
fixtures: source 1 = Alpha (clean json), source 2 = Beta (dirty jsonl),
source 3 = Gamma (xml).
"""

import app.db as db
import pytest
from app import seed
from app.db import connect
from app.modules import watermark_commit_gate
from app.repositories import settings as settings_repo
from app.repositories import sources as sources_repo
from app.services.ingest_service import run_ingest

ALPHA_MAX_TS = "2026-11-01T10:10:00Z"
BETA_MAX_TS = "2026-11-01T12:05:00Z"


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    seed.init_db()
    yield


def _set_fault(kind: str) -> None:
    conn = connect()
    try:
        settings_repo.set_value(conn, watermark_commit_gate.FAULT_SETTING_KEY, kind)
        conn.commit()
    finally:
        conn.close()


def _watermark(source_id: int) -> str | None:
    conn = connect()
    try:
        return sources_repo.get_source(conn, source_id)["watermark_ts"]
    finally:
        conn.close()


def _failed_checks(run) -> list[str]:
    return [c["name"] for c in run["report"]["gate"]["checks"] if not c["ok"]]


def test_gate_pass_commits_watermark(fresh_db):
    run = run_ingest(1, "incremental")
    assert run["status"] == "ok"
    gate = run["report"]["gate"]
    assert gate["passed"] is True
    assert all(c["ok"] for c in gate["checks"])
    assert run["report"]["candidate_watermark_ts"] == ALPHA_MAX_TS
    assert run["report"]["committed_watermark_ts"] == ALPHA_MAX_TS
    assert run["report"]["watermark_committed"] is True
    assert _watermark(1) == ALPHA_MAX_TS


def test_incremental_second_run_skips_all_and_still_passes(fresh_db):
    run_ingest(1, "incremental")
    run = run_ingest(1, "incremental")
    assert run["status"] == "ok"
    assert run["parsed_count"] == 3
    assert run["skipped_count"] == 3
    assert run["upserted_count"] == 0
    # watermark stays at the same committed position
    assert run["report"]["committed_watermark_ts"] == ALPHA_MAX_TS
    assert _watermark(1) == ALPHA_MAX_TS


def test_full_mode_gate_pass(fresh_db):
    run_ingest(1, "incremental")
    run = run_ingest(1, "full")
    assert run["status"] == "ok"
    assert run["report"]["gate"]["passed"] is True
    assert run["report"]["committed_watermark_ts"] == ALPHA_MAX_TS
    assert _watermark(1) == ALPHA_MAX_TS


def test_full_mode_does_not_wipe_watermark_on_parse_failure(fresh_db, monkeypatch):
    run_ingest(1, "incremental")
    assert _watermark(1) == ALPHA_MAX_TS

    def boom(path, fmt):
        raise ValueError("corrupt feed")

    monkeypatch.setattr("app.services.ingest_service.parse_fixture", boom)
    with pytest.raises(ValueError):
        run_ingest(1, "full")
    # pre-gate behaviour wiped the watermark up front; it must now survive
    assert _watermark(1) == ALPHA_MAX_TS


def test_gate_failure_drop_staging_row_keeps_pre_run_watermark(fresh_db):
    _set_fault("drop_staging_row")
    run = run_ingest(2, "incremental")
    assert run["status"] == "gate_failed"
    gate = run["report"]["gate"]
    assert gate["passed"] is False
    assert "staging_total_matches_parsed" in _failed_checks(run)
    # candidate was computed but must NOT be committed
    assert run["report"]["candidate_watermark_ts"] == BETA_MAX_TS
    assert run["report"]["committed_watermark_ts"] is None
    assert run["report"]["watermark_committed"] is False
    assert _watermark(2) is None
    assert run["report"]["fault_injection"]


def test_gate_failure_skewed_counter(fresh_db):
    _set_fault("skew_report_counter")
    run = run_ingest(1, "incremental")
    assert run["status"] == "gate_failed"
    assert "staging_upserts_match_report" in _failed_checks(run)
    assert _watermark(1) is None


def test_gate_failure_canonical_drift(fresh_db):
    _set_fault("canonical_drift")
    run = run_ingest(1, "incremental")
    assert run["status"] == "gate_failed"
    assert "canonical_delta_matches_inserts" in _failed_checks(run)
    assert _watermark(1) is None


def test_recovery_after_gate_failure(fresh_db):
    _set_fault("drop_staging_row")
    failed = run_ingest(2, "incremental")
    assert failed["status"] == "gate_failed"
    assert _watermark(2) is None

    _set_fault("off")
    run = run_ingest(2, "incremental")
    assert run["status"] == "ok"
    assert run["report"]["gate"]["passed"] is True
    assert run["report"]["committed_watermark_ts"] == BETA_MAX_TS
    assert _watermark(2) == BETA_MAX_TS


def test_failed_run_does_not_block_next_incremental_window(fresh_db):
    """After a gate failure the pre-run watermark is kept, so the next run
    re-processes the same window instead of skipping it as below-watermark."""
    _set_fault("drop_staging_row")
    run_ingest(1, "incremental")
    _set_fault("off")
    run = run_ingest(1, "incremental")
    # watermark was never committed, so nothing is skipped as below-watermark;
    # items land as skip_stale because canonical writes from the failed run
    # are (deliberately) not rolled back — only the watermark is held.
    assert "skipped_watermark" not in run["report"]["dispositions"]
    assert run["report"]["pre_run_watermark_ts"] is None
    assert run["status"] == "ok"
    assert run["report"]["committed_watermark_ts"] == ALPHA_MAX_TS


def test_watermark_status_consistency(fresh_db):
    run_ingest(1, "incremental")
    _set_fault("drop_staging_row")
    run_ingest(2, "incremental")

    conn = connect()
    try:
        status = {s["source_id"]: s for s in watermark_commit_gate.watermark_status(conn)}
    finally:
        conn.close()

    alpha = status[1]
    assert alpha["watermark_ts"] == ALPHA_MAX_TS
    assert alpha["last_commit"]["committed_watermark_ts"] == ALPHA_MAX_TS
    assert alpha["last_gate"]["passed"] is True
    assert alpha["consistent"] is True

    beta = status[2]
    assert beta["watermark_ts"] is None
    assert beta["last_commit"] is None
    assert beta["last_gate"]["passed"] is False
    assert beta["last_gate"]["status"] == "gate_failed"
    assert beta["consistent"] is True  # displayed watermark == last commit (both None)


def test_unknown_fault_is_ignored(fresh_db):
    _set_fault("not_a_real_fault")
    run = run_ingest(1, "incremental")
    assert run["status"] == "ok"
    assert "unknown fault" in run["report"]["fault_injection"]
    assert _watermark(1) == ALPHA_MAX_TS
