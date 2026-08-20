# Codex

Codex is the native multi-agent host for Goal Plus. It uses the shared MCP
runtime and a fixed set of parallel autonomous loops built from Codex
collaboration tools.

## Setup

```bash
python -m pip install -e ".[dev]"
cp .codex/config.example.toml .codex/config.toml
```

The local config starts:

```toml
[mcp_servers.gp-runtime]
command = "goal-plus"
args = ["--root", ".gp"]
startup_timeout_sec = 10
tool_timeout_sec = 300
enabled = true
```

Keep `.codex/config.toml` untracked and omit MCP `cwd`; `codex -C` should decide
the project root for both MCP state and hooks. The example explicitly forwards
the annotator's named environment variables. Add any custom API-key variable
named by `GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV` to the same `env_vars` list.
The same ephemeral annotator turn also produces shared-dir Tool Views; verifier
settlement never waits for it. The host-neutral lifecycle is documented in
[`shared-plane.md`](shared-plane.md#工具复制与采用结算).

Codex 0.144.1+ ships project-local Goal Plus host hooks in
`.codex/hooks.json`; each calls `goal-plus --goal-plus-host-hook` for
`UserPromptSubmit`, `SessionStart`, `PreToolUse`,
`PostToolUse`, `Stop`, and `SubagentStop`. Review and trust them through
`/hooks`. They pre-create `/goal-plus`, restore active state, gate writes/Search
calls, bind ownership, and keep a candidate alive only until its own verifier
submission is durable. Selection and final audit remain parent-owned.
`PostToolUse(goal_plus_create)` is the fallback ownership-binding path. A
candidate is blocked until its own verifier submission is durable. Ordinary subagents do not inherit the parent's next action.

Use `/goal-plus mode=autonomous <goal>` for substantial renewable candidate
exploration (the default), or `/goal-plus mode=probe <goal>` for short
feasibility/potential/blocker probes. The runtime stores the choice only as the
canonical final line of `raw_goal`.

Top-level `Stop` blocks every still-active record and re-presents the full raw
goal, timestamps, elapsed time, phase, next action, and final-check policy. The
main agent must continue or record a terminal status. Candidate `SubagentStop`
semantics are unchanged. Low-level evidence for each automatic stop-hook
invocation is stored under `.gp/host-logs/codex-hook-events/`; see
[Debugging Runtime State](debugging-runtime.md#codex). Generated HTML reports
show separate Stop/SubagentStop counts and a per-subagent breakdown from that
durable evidence.

Use the `goal-plus` skill as the user entry. The `search` skill is internal.

## Parallel Loop Flow

After planning and materializing candidates, `search_start_agent_session`
returns a launch payload like:

```json
{
  "tool": "spawn_agent",
  "task_name": "search_agent_001",
  "agent_type": "default",
  "fork_turns": "none",
  "message": "agent_session_id=agent_001; candidate_id=c001; idea: ..."
}
```

Project it onto the current tool schema. `task_name`, `message`, and the fork
field are stable. The default candidate-worker contract is self-contained in
`message`, and `agent_type="default"` selects Codex's built-in no-config role,
so the child uses native parent-model inheritance without a project role
reload. Pass other optional metadata, including `strategy.worker_launch`
overrides, only when it is present in the returned launch payload and exposed
by the current tool schema; never synthesize it from the schema alone.

### Multi-model selection

Users specify models directly in the `/goal-plus` request; they do not create
named profiles or a separate allocation object:

```text
/goal-plus models=gpt-5.6-terra,gpt-5.6-sol max_parallel=4 optimize ...
/goal-plus models=gpt-5.6-terra,gpt-5.6-sol A1B3 max_parallel=4 optimize ...
```

The first form round-robins to Terra, Sol, Terra, Sol. The second is explicit
A1B3; `models=terra*1,sol*3` is equivalent shorthand. Goal Plus calls
`goal_plus_list_models(host="codex")`, resolves each
name before freezing, and records the generated ordered `selected_models` in
the run. A selected model remains fixed for that candidate's continuations.
When `models` is absent, Codex children inherit the normal parent/default model.
The catalog is a discovery surface; dispatch still obeys the current
`spawn_agent` schema and reports a launch error if that schema cannot accept a
chosen model override.

For normal `parallel_loops`, the parent then:

1. freezes `strategy.orchestration_mode="parallel_loops"`, plans once, and
   launches all initial candidates;
2. binds returned handles with `search_bind_agent_handle`;
3. calls targetless `wait_agent` and inspects `list_agents` after each wake;
4. final-verifies every newly terminal candidate immediately;
5. observes any verifier-backed global-best update, then continues that same
   native worker with `search_continue_agent_session` followed by
   `followup_task` unless a global stop condition is true;
6. drains live agents before selection.

Every launch message says the child owns one autonomous candidate loop, not
global initial dispatch or final selection. It reads its candidate-local
context and the narrow Global Evidence view, chooses every later
hypothesis/pivot/rebase, edits its workspace, and verifies with a one-line
description of the realized attempt. A `null` View never blocks it. It must not
create candidates, select, report, promote, or mutate Goal Plus state. Main
never replaces it because of low score or one non-improving completion.

An explicitly frozen `adaptive_search` run keeps the same initial steps but may
return a runtime-owned `allocation_decision` after verifier settlement. Main
does not continue the retired worker: it idempotently applies that decision,
maps the returned derived-session launch payload to `spawn_agent`, and keeps
the live count within `max_parallel`. The child starts from the decision's
verifier-backed Git settlement and inherited run Evidence/model provenance.
Reward does not affect final hard-score selection.

## Worker Deadline

Codex has no spawn-time step or timeout field. `worker_budget` therefore
requires `max_runtime_seconds`; the adapter returns a per-dispatch two-stage
parent watchdog:

```json
{
  "budget_control": {
    "initial_wait_timeout_ms": 555000,
    "closeout_message": "Stop new work, final-verify, write the handoff, and return.",
    "final_wait_timeout_ms": 45000,
    "on_exceed": "interrupt"
  }
}
```

The parent waits until the closeout point, sends one message, waits the final
window, and interrupts only after the second timeout. `max_turns` is a hint,
not an enforceable Codex cap. A continuation may receive a larger one-dispatch
budget without changing the frozen spec.

For a sustained single-worker AutoResearch turn, add
`worker_budget.min_runtime_seconds` and optionally `min_verifier_runs`. These
are lower bounds enforced by the project `SubagentStop` hook; an early final
response receives a continuation prompt inside the same Codex worker turn, so
the main agent remains in `wait_agent`. Timing is anchored to the native child
transcript start (discovered no later than its first candidate tool or stop
event) and is recorded under
`.gp/host-logs/codex-autoresearch-leases/`. `max_runtime_seconds` remains the
parent-owned upper bound. The adapter rejects a configuration whose minimum
would still be active at the watchdog soft-closeout point. For example, a
five-minute research lease can use:

```json
{
  "min_runtime_seconds": 300,
  "min_verifier_runs": 1,
  "max_runtime_seconds": 420,
  "on_exceed": "interrupt"
}
```

This reserves 300 seconds for autonomous research, starts parent closeout at
375 seconds, and keeps the 420-second hard interrupt as a final backstop.
Workers must continue verifier-backed research rather than sleeping. A durable
`VerifierWorkspaceSideEffect` or `candidate_action=stop_and_report` releases
the lease early so infrastructure failures return to the parent immediately.

The `PostToolUse` hook may also inject one informational timing advisory when
the remaining outer/worker time is below observed verifier-submission time.
It never stops the worker. Evidence is visible through
`goal_plus_monitor_snapshot`.

For one worker, call `search_get_agent_observability(agent_session_id)`. Codex
uses the `SubagentStop.agent_transcript_path` when present and otherwise
discovers the native `${CODEX_HOME:-~/.codex}/sessions/...` JSONL from the
bound unique task name. The normalized result includes resolved model and
reasoning effort, active and wall duration, terminal state, token/context
counts, tool/message counts, and the session artifact path. It intentionally
omits prompt, reasoning, and tool payload content. Interrupted workers that do
not emit `SubagentStop` remain discoverable by task name.

## Resume

Prefer native continuation while the Codex worker remains available:

```text
search_continue_agent_session -> followup_task
```

The follow-up directive is neutral: refresh durable context and continue the
same autonomous loop. Main does not supply a preferred technical direction.

If the worker is gone or a fresh context is intentional, use
`search_redispatch_candidate`. The new session uses the same workspace, Git
state, verifier history, and structured `.tmp/handoff.json`.

## Supported Strategies

Codex supports the portable builtin set:

- `agent_guided`, `agent`, `default`
- `random`, `random_mode`

See [Agent Host Adapters](agent-host-adapters.md) before enabling another
strategy.

## Verification

Fast contract tests:

```bash
pytest -m codex -q
```

The dedicated parallel-loop scenario is verified with `gpt-5.6-luna`:

```bash
ST_CODEX_MODEL=gpt-5.6-luna \
  pytest -m "st and st_codex" -k codex_parallel_loop_cycle -v -s -rs
```

`codex_parallel_loop_cycle` proves one initial plan, wait-any behavior,
same-worker follow-up on the same candidate, verifier-backed best updates, and
final selection/promotion after drain. Goal Plus report generation happens once
after the parent record reaches a terminal status.

## Logs

```bash
codex exec --json ... > .gp/host-logs/codex-run.jsonl
RUST_LOG=debug codex -c log_dir=./.codex-log
```

Persisted transcripts are under
`${CODEX_HOME:-~/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl`. Use JSON or
rollout logs when you need tool calls; `codex exec -o` contains only the final
message. Cross-host diagnosis is documented in
[Debugging](debugging-runtime.md).
