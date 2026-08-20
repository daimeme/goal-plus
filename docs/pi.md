# Pi

Pi support is project-local: one user-facing `goal-plus` skill under
`.pi/skills/goal-plus/`, one extension, and Python worker/facade commands. It
does not patch Pi core and does not expose a separate user-facing `search` skill.

## Setup

```bash
python -m pip install -e ".[dev]"
pi -p "/goal-plus inspect this repository"
```

The extension provides pre-model `/goal-plus` creation in interactive,
RPC, print, and JSON modes. It persists the active id when the Pi session is
persistent, injects hidden context, gates selected writes/Search calls, and
runs a native turn-level stop gate. This is no host process Stop hook.

For Pi Search runs, asynchronous Evidence annotation also stays on Pi. The
run-scoped drainer uses an ephemeral `pi --mode json --no-session --no-tools`
process, inherits the Pi worker model/provider unless explicitly overridden,
and reads provider configuration from `PI_CODING_AGENT_DIR`. A qualified
annotator model (`provider/model`) or `evidence_annotator.pi_provider` can select
a provider independent of the Search worker. Verifier settlement never waits
for this process. The same turn also produces shared-dir Tool Views. The
host-neutral lifecycle is documented in
[`shared-plane.md`](shared-plane.md#工具复制与采用结算).

`/goal-plus edit`, `/goal-plus resume`, and `/goal-plus-with-final-check` share
the same goal revision semantics as Codex. Required checks run through a
separate read-only Pi RPC reviewer.

Use `/goal-plus mode=autonomous <goal>` for substantial renewable candidate
exploration (the default), or `/goal-plus mode=probe <goal>` for short
feasibility/potential/blocker probes. The choice is normalized into the final
line of `raw_goal`; it is not a Pi pool or Search runtime state.

At the end of a main turn, every still-active record is continued with its full
raw goal and elapsed-time context. Pi stops only after the agent records a
terminal status. Worker watchdog expiry remains a dispatch event, not goal
completion.

At terminal state, Pi writes a visible `Goal Plus stats` custom entry with
elapsed time, messages, tool calls, token use, and estimated cost. It is not an
LLM message and does not trigger another assistant turn.

## How Pi Differs From Codex

The main agent uses extension events rather than project hook files. Candidate
workers are Pi RPC processes supervised by a durable host-local pool. Each
detached wrapper owns one foreground `pi --mode rpc` child launched by
`goal-plus-pi-worker`; native session JSONL lives under
`.gp/host-sessions/pi/`.

Pool state lives under `.gp/host-pools/pi/`; Search records remain host-neutral.
`pi_search_pool_continue` starts a new process that reloads the same native Pi
session in the same candidate workspace and preserves `agent_session_id`.
Metrics use `get_entries(since=<last_entry_id>)`, so each dispatch transfers
only new entries while cumulative usage remains available in the bound handle.
This does not keep one OS process resident across completions.
The resume launch explicitly resets dispatch-scoped deadline semantics: a
closeout or time advisory persisted by an earlier process is historical, and
only warnings delivered after the latest launch apply to the new budget.
Each resume intentionally uses a new process; native session state and the
candidate workspace provide continuity without requiring a persistent worker
PID.

## Worker Spec

Use `worker_host="pi-rpc"` and a wall-clock budget:

```json
{
  "strategy": {
    "name": "random",
    "orchestration_mode": "parallel_loops",
    "worker_host": "pi-rpc",
    "worker_budget": {
      "min_runtime_seconds": 500,
      "min_verifier_runs": 1,
      "max_runtime_seconds": 600,
      "max_turns": 8,
      "on_exceed": "interrupt"
    }
  }
}
```

`max_runtime_seconds` is required. Optional `min_runtime_seconds` and
`min_verifier_runs` are enforced cumulatively by the pool wrapper: an early Pi
turn restarts the same native session in the same slot/worktree with only the
remaining upper budget. Infrastructure failure, pool close, or outer closeout
stops this automatic continuation. Before each remaining hard limit, the runner
sends one closeout steer. `max_turns` is only a prompt hint.

### Explicit role models

The native Pi command can select the main, Evidence Annotation, and Search
worker models with role-named parameters:

```text
/goal-plus main=A annotator=B workers=C,D max_parallel=4 optimize ...
```

`main=` switches the current Pi model before the first model turn.
`annotator=` freezes `strategy.evidence_annotator.model`. `workers=` resolves
one or more models into `strategy.models`; uncounted entries round-robin and
counted entries retain the allocation rules below. Omitted roles keep their
existing host default or inheritance behavior rather than inheriting from an
unrelated explicit role.

Use qualified `provider/model` references when an id exists under more than one
provider. Existing `models=` remains a compatibility alias for `workers=` so
older commands retain exactly the same Candidate allocation semantics. Do not
specify both aliases in one request.

### Multi-model selection

Use `workers` (or the compatibility alias `models`) directly in the
natural-language command. Because Pi can expose
the same model id from multiple providers, canonical `provider/model` values
are the clearest form:

```text
/goal-plus workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol max_parallel=4 optimize ...
/goal-plus workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol A1B3 max_parallel=4 optimize ...
```

Goal Plus obtains the catalog from Pi RPC `get_available_models` through
`goal_plus_list_models(host="pi-rpc")`. A short id is accepted only when it
matches one entry uniquely. Uncounted entries round-robin; `A1B3` (or
`A*1,B*3`) becomes explicit counts that must sum to `max_parallel`. The
runtime-generated `selected_models` are immutable
per candidate/native session. Omitting both `workers` and `models` keeps Pi's
current default.

## Parallel Loops

Normal Pi Search follows the [Shared Plane](shared-plane.md) flow:

1. set `orchestration_mode="parallel_loops"`, then plan and materialize the
   initial candidates exactly once;
2. `pi_search_pool_open(..., max_parallel=<frozen limit>)`;
3. `pi_search_pool_wait_any` for the first terminal event;
4. for `candidate_ready`, observe any verifier-backed best update and call
   `pi_search_pool_continue` for that exact candidate unless a global stop
   condition is true; treat `timed_out`, `interrupted`, and `failed` as
   incomplete rather than continuing them as ready candidates;
5. recover interrupted main turns with `pi_search_pool_snapshot(run_id=...)`;
6. `pi_search_pool_close`, then select and promote;
7. record the Search result, finish the raw-goal audit, set a terminal Goal Plus
   status, then generate the final report exactly once per recorded run.

The supervisor enforces `max_parallel` and never auto-refills. Main never calls
submit after initial pool creation and never replaces a candidate because of
low score or lack of improvement. A `candidate_ready` event is published only
after the driver has bound the handle, released any minimum lease, and confirmed
durable Evidence for the current artifact. An exhausted unsatisfied lease emits
`timed_out` instead. While a minimum lease is active, Pi `agent_end` queues the
next turn in the same process and native session until the configured closeout
point. When the last assistant response ends with `stopReason="length"` and has
no tool call, the worker-local extension does not continue that context. The
driver returns a refresh signal and the pool supervisor creates a new native
session with a new `agent_session_id` for the same candidate and workspace. The
main agent is not involved, and workspace changes plus durable Evidence survive
the refresh. The driver reuses matching worker Evidence instead of adding a
duplicate parent process iteration. Parent verification is only a fallback for
a changed, unverified artifact; an unchanged workspace does not create a baseline
iteration.

There is no public synchronous candidate/batch runner. Pool open owns the
initial fixed lane set; pool continue owns later dispatches for those same
lanes.

For an explicitly frozen `adaptive_search` run, the runtime may retire a lane
and return a persisted expansion decision. This releases any lower-bound lease
for that retired lane. Main applies the exact decision, checks aggregate active
jobs across all known pools, and opens a new one-candidate pool with
`max_parallel=1` for the returned candidate only when a slot is available.
There is still no public pool-submit tool and the supervisor never chooses or
auto-refills candidates. `budget.max_candidates` limits total materialized
candidates; final selection still uses the hard verifier metric.

## Worker Boundary

Worker-role extension tools are limited to `search_get_agent_context`,
`search_get_global_evidence`,
`search_run_verifier`, and `search_list_iterations`. Each iteration reads the
Global Evidence view, independently chooses a direction, edits only inside the
returned workspace, runs the verifier with a one-line hypothesis describing the
realized attempt, and updates a bounded `.tmp/handoff.json`. A `null` View means
the annotator has not published yet and never requires waiting.

The persisted native session is the normal continuation surface. The handoff,
candidate Git state, and `.gp` verifier history remain the durable recovery
surface when native session loading is unavailable.

Every redispatched worker owns the next hypothesis, pivot, and rebase within
the same candidate workspace. Main sends a neutral continuation directive and
does not act as a technical conductor.

## Tool Facade

`goal-plus-pi-tool` exposes the same GoalPlusTools/SearchTools facade plus the
Pi-local pool tools:

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

`goal_plus_monitor_snapshot` is read-only and also exists on MCP. It never
starts, waits for, or stops a worker. The complete concise tool index is in
[API](api.md).

Use `search_get_agent_observability(agent_session_id)` for the same normalized
per-worker schema used by Codex. Pi maps the existing `pi_metrics` model,
thinking level, duration, usage/cost, context, and log/session paths into that
schema; the legacy bound fields remain readable.

## State And Logs

Search state, candidate commits, and workspaces under `.gp/` are authoritative.
Worker logs default to a metadata-only event log:

```text
.gp/host-logs/pi-rpc-<agent_session_id>.jsonl
```

It stores event/tool status, bounded errors, timing, and usage without prompts
or reasoning. Set `GOAL_PLUS_PI_RAW_LOG=1` only for focused debugging; raw
streams can become very large.

Bound handles include `metadata.pi_metrics` (including resolved model and
thinking level), timeout/failure evidence, and a
bounded `metadata.progress_handoff`. A timeout is successful deadline
enforcement; runner failure is recorded separately with synthetic failure
metadata so monitoring never mistakes it for a live session.

## Supported Strategies

Pi currently supports the portable builtin strategies only:

- `agent_guided`, `agent`, `default`
- `random`, `random_mode`

## Verification

```bash
pytest -m pi -q
ST_PI_CYCLE_WORKER_SECONDS=120 \
  pytest -m "st and st_pi_rpc" -k managed_pool_wait_any -v -s -rs
```

The real-host test launches two detached Pi RPC workers, rediscovers the pool
by `run_id`, observes wait-any completion, and drains cleanly. See
[Debugging](debugging-runtime.md) for cross-host diagnosis.
