from pathlib import Path

import pytest

from goal_plus.shared_dir_trace import (
    append_shared_dir_trace,
    shared_dir_trace_path,
    summarize_shared_dir_trace,
)


def test_shared_dir_trace_is_disabled_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOAL_PLUS_SHARED_DIR_TRACE", raising=False)
    append_shared_dir_trace(tmp_path, "run_0001", "stage_requested")

    assert not shared_dir_trace_path(tmp_path, "run_0001").exists()
    assert summarize_shared_dir_trace(tmp_path, "run_0001") is None


def test_shared_dir_trace_reader_tolerates_a_malformed_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOAL_PLUS_SHARED_DIR_TRACE", "1")
    append_shared_dir_trace(
        tmp_path,
        "run_0001",
        "stage_requested",
        candidate_id="c001",
    )
    path = shared_dir_trace_path(tmp_path, "run_0001")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{incomplete\n")

    summary = summarize_shared_dir_trace(tmp_path, "run_0001")

    assert summary is not None
    monkeypatch.delenv("GOAL_PLUS_SHARED_DIR_TRACE")
    summary = summarize_shared_dir_trace(tmp_path, "run_0001")
    assert summary is not None
    assert summary["collection_enabled"] is True
    assert summary["events_total"] == 1
    assert summary["malformed_lines"] == 1
    assert summary["counts"]["stage_attempts"] == 1
