# Model Optimize Example

This scenario demonstrates a single-level model optimization flow for
`/goal-plus`.

It is intentionally not a nested orchestration framework. Goal Plus owns one
top-level goal record, Search Mode owns one bounded local search when a
verifier-backed `SearchSpec` is ready, and model-optimize-specific state stays
scenario-local.

## Current Target

The runnable target is `torch-cpu-target/`, a deterministic single-core PyTorch
workspace. A user should be able to point `/goal-plus` at that workspace with a
short optimization request. Goal Plus should inspect the files, discover the
metric/verifier/edit surface, and open Search Mode only if the task is ready.

The target deliberately contains:

- a fused-vector opportunity in `model.py`
- a redundant projection in `serving.py`
- `verify.py` as the correctness gate
- `benchmark.py` as the `tokens_per_second` metric
- `profile.py` as optional evidence for the main agent
- `cpp_reference/fused_vector_tail.cpp` as a known-good C++ CPU operator pattern

Every script forces PyTorch and common CPU backends to one CPU thread. Raising
the CPU thread count is not a valid optimization.

## Files

- `design.md`: scenario design and Goal Plus boundary rationale
- `plan.md`: staged implementation plan
- `torch-cpu-target/`: runnable PyTorch CPU workspace
- `torch-cpu-target/multi-model-search-spec.json`: two-slot static Terra/Sol
  model selection under shared Annotated Evidence
- `torch-cpu-target/multi-model-run.md`: `scaling` Conda and
  Codex CLI runbook with runtime-evidence acceptance criteria
- `torch-cpu-target/adaptive-search-spec.json`: Reward V2, durable value backup,
  and value-guided allocation validation
- `pi-goal-prompt.md`: minimal user-level `/goal-plus` request

## Boundary

- Keep domain knowledge external to Goal Plus.
- Keep domain guidance in this example prompt and workspace docs, not in an
  additional Pi skill.
- Let Goal Plus discover the SearchSpec from the workspace and goal prompt.
- Keep verifier evidence separate from local Search scores.
- Do not add nested scheduling, worker supervision, or hardware-specific logic
  to Goal Plus for this scenario.
