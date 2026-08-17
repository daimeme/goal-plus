from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any
import uuid


SHARED_DIR_TRACE_ENV = "GOAL_PLUS_SHARED_DIR_TRACE"
SHARED_DIR_TRACE_SCHEMA_VERSION = 1
SHARED_DIR_TRACE_RELATIVE_PATH = Path("debug") / "shared-dir-events.jsonl"
SHARED_DIR_TRACE_MAX_PATHS = 200


def shared_dir_trace_enabled() -> bool:
    return os.environ.get(SHARED_DIR_TRACE_ENV) == "1"


def shared_dir_trace_path(root_dir: Path | str, run_id: str) -> Path:
    return Path(root_dir).resolve() / "runs" / run_id / SHARED_DIR_TRACE_RELATIVE_PATH


def append_shared_dir_trace(
    root_dir: Path | str,
    run_id: str,
    event: str,
    *,
    candidate_id: str | None = None,
    agent_session_id: str | None = None,
    iteration: int | None = None,
    tool_id: str | None = None,
    snapshot_hash: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append best-effort diagnostic metadata without affecting run state."""
    if not shared_dir_trace_enabled():
        return
    try:
        # Imported lazily to keep the trace reader independent from runtime.py.
        from goal_plus.runtime import exclusive_file_lock, utc_timestamp

        path = shared_dir_trace_path(root_dir, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": SHARED_DIR_TRACE_SCHEMA_VERSION,
            "event_id": f"sdt_{uuid.uuid4().hex[:20]}",
            "event": event,
            "created_at": utc_timestamp(),
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": agent_session_id,
            "iteration": iteration,
            "tool_id": tool_id,
            "snapshot_hash": snapshot_hash,
            "payload": payload or {},
        }
        line = json.dumps(record, sort_keys=True, ensure_ascii=True, default=str)
        with exclusive_file_lock(path.with_suffix(".lock")):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
    except Exception:
        # Debug evidence must never change Search behavior or settlement.
        return


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _duration(start: Any, end: Any) -> float | None:
    start_epoch = _parse_timestamp(start)
    end_epoch = _parse_timestamp(end)
    if start_epoch is None or end_epoch is None or end_epoch < start_epoch:
        return None
    return round(end_epoch - start_epoch, 3)


def _latency_summary(values: list[float]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[p95_index], 3),
        "max": round(ordered[-1], 3),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def load_shared_dir_trace(path: Path) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    malformed = 0
    try:
        stream = path.open("r", encoding="utf-8")
    except OSError:
        return events, malformed
    with stream:
        for sequence, line in enumerate(stream):
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                malformed += 1
                continue
            if not isinstance(item, dict) or not isinstance(item.get("event"), str):
                malformed += 1
                continue
            if not isinstance(item.get("payload", {}), dict):
                malformed += 1
                continue
            item["_sequence"] = sequence
            events.append(item)
    events.sort(
        key=lambda item: (
            _parse_timestamp(item.get("created_at")) or 0.0,
            int(item.get("_sequence") or 0),
        )
    )
    return events, malformed


def summarize_shared_dir_trace(
    root_dir: Path | str,
    run_id: str,
    *,
    max_paths: int = SHARED_DIR_TRACE_MAX_PATHS,
) -> dict[str, Any] | None:
    path = shared_dir_trace_path(root_dir, run_id)
    if not path.is_file():
        return None
    events, malformed = load_shared_dir_trace(path)

    stages_ok = [
        item
        for item in events
        if item["event"] == "stage_result"
        and item.get("payload", {}).get("status") == "ok"
    ]
    published = {
        str(item["tool_id"]): item
        for item in events
        if item["event"] == "snapshot_published" and item.get("tool_id")
    }
    views = {
        str(item["tool_id"]): item
        for item in events
        if item["event"] == "tool_view_bound" and item.get("tool_id")
    }

    visible_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for item in events:
        if item["event"] != "evidence_returned" or not item.get("candidate_id"):
            continue
        reader = str(item["candidate_id"])
        tools = item.get("payload", {}).get("visible_tools")
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("tool_id"):
                continue
            source_candidate = tool.get("source_candidate_id")
            if source_candidate == reader:
                continue
            key = (reader, str(tool["tool_id"]))
            visible_pairs.setdefault(
                key,
                {
                    "candidate_id": reader,
                    "tool_id": str(tool["tool_id"]),
                    "source_candidate_id": source_candidate,
                    "first_visible_at": item.get("created_at"),
                    "evidence_reads_with_tool": 0,
                },
            )
            visible_pairs[key]["evidence_reads_with_tool"] += 1

    copy_attempts = [item for item in events if item["event"] == "copy_requested"]
    copies = {
        str(item.get("payload", {}).get("receipt_id")): item
        for item in events
        if item["event"] == "copy_result"
        and item.get("payload", {}).get("status") == "ok"
        and item.get("payload", {}).get("receipt_id")
    }
    copy_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for item in copies.values():
        if item.get("candidate_id") and item.get("tool_id"):
            copy_by_pair.setdefault(
                (str(item["candidate_id"]), str(item["tool_id"])), item
            )

    consumed = {
        str(item.get("payload", {}).get("receipt_id")): item
        for item in events
        if item["event"] == "receipt_consumed"
        and item.get("payload", {}).get("receipt_id")
    }
    for pair in visible_pairs.values():
        later_verifiers = [
            item
            for item in events
            if item["event"] == "verifier_settled"
            and item.get("candidate_id") == pair["candidate_id"]
            and _duration(pair["first_visible_at"], item.get("created_at")) is not None
            and item.get("created_at") != pair["first_visible_at"]
        ]
        pair["later_verifier_count"] = len(later_verifiers)
        pair["first_later_verifier_at"] = (
            later_verifiers[0].get("created_at") if later_verifiers else None
        )
    actionable_visible_pairs = {
        key: pair
        for key, pair in visible_pairs.items()
        if pair["later_verifier_count"] > 0
    }
    adoption_events = [
        item
        for item in events
        if item["event"] == "verifier_settled"
        and item.get("payload", {}).get("adopted_receipt_ids")
    ]
    single_non_confounded_adoptions = [
        item
        for item in adoption_events
        if item.get("payload", {}).get("adoption_confounded") is False
        and len(item.get("payload", {}).get("adopted_receipt_ids") or []) == 1
    ]
    isolated_adoptions = [
        item
        for item in single_non_confounded_adoptions
        if item.get("payload", {}).get("baseline_score") is not None
    ]
    valid_adoptions = [
        item for item in adoption_events if item.get("payload", {}).get("process_passed") is True
    ]
    non_degrading_adoptions = [
        item
        for item in adoption_events
        if item.get("payload", {}).get("disposition") in {"keep", "retain"}
    ]
    improving_adoptions = [
        item
        for item in adoption_events
        if item.get("payload", {}).get("disposition") == "keep"
        and item.get("payload", {}).get("baseline_score") is not None
    ]
    isolated_improving_adoptions = [
        item for item in isolated_adoptions if item in improving_adoptions
    ]

    snapshot_to_view: list[float] = []
    view_to_visibility: list[float] = []
    visibility_to_copy: list[float] = []
    copy_to_consumption: list[float] = []
    for tool_id, publish_event in published.items():
        view_event = views.get(tool_id)
        latency = _duration(
            publish_event.get("created_at"),
            view_event.get("created_at") if view_event else None,
        )
        if latency is not None:
            snapshot_to_view.append(latency)
        peer_reads = [
            pair
            for pair in visible_pairs.values()
            if pair["tool_id"] == tool_id
        ]
        if view_event and peer_reads:
            first_read = min(
                peer_reads,
                key=lambda pair: _parse_timestamp(pair["first_visible_at"]) or float("inf"),
            )
            latency = _duration(view_event.get("created_at"), first_read["first_visible_at"])
            if latency is not None:
                view_to_visibility.append(latency)
    for key, pair in visible_pairs.items():
        copy_event = copy_by_pair.get(key)
        latency = _duration(
            pair["first_visible_at"],
            copy_event.get("created_at") if copy_event else None,
        )
        if latency is not None:
            visibility_to_copy.append(latency)
    for receipt_id, copy_event in copies.items():
        consumed_event = consumed.get(receipt_id)
        latency = _duration(
            copy_event.get("created_at"),
            consumed_event.get("created_at") if consumed_event else None,
        )
        if latency is not None:
            copy_to_consumption.append(latency)

    candidate_ids = sorted(
        {
            str(item["candidate_id"])
            for item in events
            if item.get("candidate_id")
        }
    )
    by_candidate: dict[str, dict[str, Any]] = {}
    for candidate_id in candidate_ids:
        candidate_events = [
            item for item in events if item.get("candidate_id") == candidate_id
        ]
        candidate_adoptions = [
            item for item in adoption_events if item.get("candidate_id") == candidate_id
        ]
        visible_tool_ids = sorted(
            tool_id
            for reader, tool_id in visible_pairs
            if reader == candidate_id
        )
        by_candidate[candidate_id] = {
            "evidence_reads": sum(
                item["event"] == "evidence_returned" for item in candidate_events
            ),
            "visible_peer_tool_ids": visible_tool_ids,
            "visibility_pairs_followed_by_verifier": sum(
                pair["candidate_id"] == candidate_id
                and pair["later_verifier_count"] > 0
                for pair in visible_pairs.values()
            ),
            "stage_attempts": sum(
                item["event"] == "stage_requested" for item in candidate_events
            ),
            "published_tool_ids": sorted(
                tool_id
                for tool_id, item in published.items()
                if item.get("candidate_id") == candidate_id
            ),
            "copied_receipts": sum(
                item.get("candidate_id") == candidate_id for item in copies.values()
            ),
            "consumed_receipts": sum(
                item.get("candidate_id") == candidate_id for item in consumed.values()
            ),
            "adoption_iterations": len(candidate_adoptions),
            "isolated_adoption_iterations": sum(
                item in isolated_adoptions for item in candidate_adoptions
            ),
            "improving_adoption_iterations": sum(
                item in improving_adoptions for item in candidate_adoptions
            ),
        }

    tool_paths: list[dict[str, Any]] = []
    for tool_id, publish_event in published.items():
        peer_pairs = sorted(
            (
                (key, pair)
                for key, pair in visible_pairs.items()
                if pair["tool_id"] == tool_id
            ),
            key=lambda item: item[0],
        )
        if not peer_pairs:
            tool_paths.append(
                {
                    "tool_id": tool_id,
                    "source_candidate_id": publish_event.get("candidate_id"),
                    "snapshot_published_at": publish_event.get("created_at"),
                    "tool_view_bound_at": views.get(tool_id, {}).get("created_at"),
                    "peer_candidate_id": None,
                    "first_visible_at": None,
                    "evidence_reads_with_tool": 0,
                    "later_verifier_count": 0,
                    "first_later_verifier_at": None,
                    "copied_at": None,
                    "receipt_consumed_at": None,
                    "adoption_disposition": None,
                    "adoption_confounded": None,
                    "adoption_baseline_score": None,
                    "adoption_score_delta": None,
                }
            )
            continue
        for key, pair in peer_pairs:
            copy_event = copy_by_pair.get(key)
            receipt_id = (
                copy_event.get("payload", {}).get("receipt_id")
                if copy_event is not None
                else None
            )
            consumed_event = consumed.get(str(receipt_id)) if receipt_id else None
            tool_paths.append(
                {
                    "tool_id": tool_id,
                    "source_candidate_id": publish_event.get("candidate_id"),
                    "snapshot_published_at": publish_event.get("created_at"),
                    "tool_view_bound_at": views.get(tool_id, {}).get("created_at"),
                    "peer_candidate_id": pair["candidate_id"],
                    "first_visible_at": pair["first_visible_at"],
                    "evidence_reads_with_tool": pair["evidence_reads_with_tool"],
                    "later_verifier_count": pair["later_verifier_count"],
                    "first_later_verifier_at": pair["first_later_verifier_at"],
                    "copied_at": copy_event.get("created_at") if copy_event else None,
                    "receipt_consumed_at": (
                        consumed_event.get("created_at") if consumed_event else None
                    ),
                    "adoption_disposition": (
                        consumed_event.get("payload", {}).get("disposition")
                        if consumed_event
                        else None
                    ),
                    "adoption_confounded": (
                        consumed_event.get("payload", {}).get("adoption_confounded")
                        if consumed_event
                        else None
                    ),
                    "adoption_baseline_score": (
                        consumed_event.get("payload", {}).get("baseline_score")
                        if consumed_event
                        else None
                    ),
                    "adoption_score_delta": (
                        consumed_event.get("payload", {}).get("score_delta")
                        if consumed_event
                        else None
                    ),
                }
            )
    tool_paths.sort(
        key=lambda item: (
            str(item.get("snapshot_published_at") or ""),
            str(item.get("tool_id") or ""),
            str(item.get("peer_candidate_id") or ""),
        )
    )

    counts = {
        "stage_attempts": sum(item["event"] == "stage_requested" for item in events),
        "staged_tools": len(stages_ok),
        "verifier_settlements": sum(
            item["event"] == "verifier_settled" for item in events
        ),
        "published_tools": len(published),
        "tool_views_bound": len(views),
        "tool_view_failures": sum(
            item["event"] == "tool_view_failed" for item in events
        ),
        "evidence_reads": sum(
            item["event"] == "evidence_returned" for item in events
        ),
        "peer_visibility_pairs": len(visible_pairs),
        "visibility_pairs_followed_by_verifier": len(actionable_visible_pairs),
        "visibility_pairs_without_later_verifier": (
            len(visible_pairs) - len(actionable_visible_pairs)
        ),
        "copy_attempts": len(copy_attempts),
        "copied_receipts": len(copies),
        "consumed_receipts": len(consumed),
        "adoption_iterations": len(adoption_events),
        "single_non_confounded_adoption_iterations": len(
            single_non_confounded_adoptions
        ),
        "isolated_adoption_iterations": len(isolated_adoptions),
        "valid_adoption_iterations": len(valid_adoptions),
        "non_degrading_adoption_iterations": len(non_degrading_adoptions),
        "improving_adoption_iterations": len(improving_adoptions),
        "isolated_improving_adoption_iterations": len(
            isolated_improving_adoptions
        ),
    }
    counts["stage_requests_without_result"] = max(
        counts["stage_attempts"] - counts["staged_tools"], 0
    )
    counts["copy_requests_without_result"] = max(
        counts["copy_attempts"] - counts["copied_receipts"], 0
    )
    return {
        "schema_version": SHARED_DIR_TRACE_SCHEMA_VERSION,
        "collection_enabled": True,
        "trace_path": str(path),
        "events_total": len(events),
        "malformed_lines": malformed,
        "counts": counts,
        "rates": {
            "stage_to_snapshot": _rate(counts["published_tools"], counts["staged_tools"]),
            "snapshot_to_tool_view": _rate(
                counts["tool_views_bound"], counts["published_tools"]
            ),
            "visibility_to_copy": _rate(
                len(copy_by_pair), counts["peer_visibility_pairs"]
            ),
            "followed_visibility_to_copy": _rate(
                sum(key in copy_by_pair for key in actionable_visible_pairs),
                counts["visibility_pairs_followed_by_verifier"],
            ),
            "copy_to_receipt_consumption": _rate(
                counts["consumed_receipts"], counts["copied_receipts"]
            ),
            "isolated_to_improvement": _rate(
                counts["isolated_improving_adoption_iterations"],
                counts["isolated_adoption_iterations"],
            ),
        },
        "latencies_seconds": {
            "snapshot_to_tool_view": _latency_summary(snapshot_to_view),
            "tool_view_to_first_peer_visibility": _latency_summary(view_to_visibility),
            "first_visibility_to_copy": _latency_summary(visibility_to_copy),
            "copy_to_receipt_consumption": _latency_summary(copy_to_consumption),
        },
        "by_candidate": by_candidate,
        "tool_paths": tool_paths[:max_paths],
        "tool_paths_total": len(tool_paths),
        "tool_paths_truncated": len(tool_paths) > max_paths,
        "interpretation": (
            "copy and receipt consumption prove snapshot delivery only; they do not prove "
            "that peer code was executed, adapted, or retained"
        ),
    }
