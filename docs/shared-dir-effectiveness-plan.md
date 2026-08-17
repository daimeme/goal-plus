# Shared-dir Effectiveness Validation Plan

This plan validates shared-dir in three separate layers. It deliberately keeps
mechanism correctness, natural adoption opportunity, and causal benchmark
effect apart. A passing mechanism example is necessary but is not evidence
that autonomous workers benefit in ordinary tasks.

The initial campaign is observational. Do not change shared-dir publication,
discoverability, copy, receipt, ranking, or worker-loop semantics until Layers
1 and 2 have produced an interpretable funnel. Debug-only fixes are allowed if
they do not change what workers can see or do.

## Questions And Hypotheses

The campaign answers these questions in order:

1. Does every runtime-owned transition complete and leave exact durable
   evidence?
2. In ordinary multi-agent work, how often is a useful snapshot published in
   time for a peer to see it and still run another verifier?
3. Conditional on visibility, how often does a peer copy and settle a receipt?
4. Conditional on an isolated adoption trial with a prior baseline, how often
   does the candidate improve?
5. After all opportunity and overhead effects are included, does shared-dir
   improve paired benchmark outcomes?

Classify failures before proposing changes:

| Class | Observable condition | Interpretation |
|---|---|---|
| Supply | few successful stages | workers did not produce shareable tools |
| Publication | staged but no snapshot | staging, verifier, or snapshot settlement failed |
| Discoverability | snapshot but no bound Tool View | annotation failed or completed too late |
| Opportunity | bound View but no peer visibility pair | peers did not read again before stopping |
| Demand | visible but not copied | the peer declined or ignored an available tool |
| Consumption | copied but receipt not consumed | no subsequent attributed verifier settled it |
| Attribution | receipt consumed in a confounded trial | score movement cannot be assigned to the tool |
| Utility | isolated valid adoption did not improve | the adopted material had no measured marginal gain |
| End-to-end | conditional utility exists but treatment loses | overhead or low opportunity frequency dominates |

## Diagnostic Contract

Set `GOAL_PLUS_SHARED_DIR_TRACE=1` in the environment of the MCP server. The
evidence annotator inherits the same environment. The default remains off.

PowerShell example:

```powershell
$env:GOAL_PLUS_SHARED_DIR_TRACE = "1"
goal-plus --root .gp
```

The runtime appends metadata-only events to:

```text
.gp/runs/<run_id>/debug/shared-dir-events.jsonl
```

The trace is ignored local state. It must not be committed. It contains IDs,
timestamps, counts, dispositions, bounded settlement/annotation errors, and
relative draft paths. It does not contain source contents, prompts,
transcripts, hidden answers, or
worker reasoning. It is not read by workers and does not affect Evidence,
selection, promotion, or reports.

`goal_plus_monitor_snapshot` adds `run.shared_dir_trace` when the trace file
exists. That object contains stage counts, conversion rates, latency summaries,
per-candidate totals, and bounded candidate-tool paths. The raw JSONL remains
the authoritative diagnostic record.

The event chain is:

```text
stage_requested -> stage_result
  -> verifier_settled -> snapshot_published
  -> tool_view_started -> tool_view_bound | tool_view_retry | tool_view_failed
  -> evidence_returned(with visible_tools)
  -> copy_requested -> copy_result
  -> verifier_settled -> receipt_consumed
```

A requested stage or copy without its matching success result is classified as
`stage_requests_without_result` or `copy_requests_without_result`. During an
active call this is only pending; after workers are quiet it is a failed or
interrupted call whose exact tool error should be read from the host-native log.

Use the following symbols when reporting the funnel:

| Symbol | Monitor count | Unit |
|---|---|---|
| S | `staged_tools` | successful stage calls |
| P | `published_tools` | unique immutable snapshots |
| V | `tool_views_bound` | unique snapshots with bound Tool Views |
| O | `peer_visibility_pairs` | unique peer candidate/tool pairs returned in Evidence |
| O+ | `visibility_pairs_followed_by_verifier` | visibility pairs followed by another settled peer verifier |
| C | `copied_receipts` | successful copy receipts |
| R | `consumed_receipts` | receipts consumed by an attributed verifier |
| I | `isolated_adoption_iterations` | one-receipt, non-confounded adoption iterations with a prior baseline |
| G | `isolated_improving_adoption_iterations` | isolated adoptions improving over a prior baseline |

Report `P/S`, `V/P`, `O+/O`, `C/O`, `C/O+`, `R/C`, and `G/I` separately. Never call `C` or `R`
proof that tool code was executed, adapted, or retained. Actual use remains
unknown unless independent artifact evidence or a later structured experiment
establishes it.

Also report these latency distributions: snapshot to Tool View, Tool View to
first peer visibility, first visibility to copy, and copy to receipt
consumption. Report count, minimum, median, p95, and maximum. A latency without
a downstream event is censored, not zero.

## Reproducibility Ledger

For every run preserve, outside worker-visible workspaces:

- benchmark and case identity, including the repository case manifest when one
  exists;
- source and benchmark harness commits;
- frozen spec hash and verifier hashes;
- host, exact model reference, adapter version, strategy, initial proposals,
  and seed;
- candidate count, worker budget, outer deadline, evidence mode, and shared-dir
  limits;
- run ID, trace path, monitor snapshot, terminal report paths, and infrastructure
  failure classification;
- observed token, cost, duration, verifier, and annotation counts when available.

Do not compare runs whose task identity, verifier contract, source commit, or
model/budget pairing differs. Do not replace failed cases with easier cases.
Infrastructure failures remain in the ledger and are rerun under a predeclared
retry rule.

## Layer 1: Mechanism Validation

### Purpose

Prove that the instrumented runtime chain works and that the funnel agrees with
durable Search state. This layer does not estimate autonomous usefulness.

### Workload

Use the checked-in model-optimize shared-dir scenario with its fixed publisher
and consumer roles. Run the normal `parallel_loops` host path; do not edit
frozen verifiers, candidate workspaces, or `.gp` records by hand.

Run at least three clean repetitions on the primary host. Before cross-host
claims, run at least one successful repetition on each maintained host used in
the later campaign. Keep one CPU thread and the verifier resource lock.

### Required Assertions

Each successful repetition must show:

1. one successful stage and an attributed passing producer verifier;
2. one immutable snapshot and the matching shared index entry;
3. no peer-visible tool before Tool View binding;
4. a bound View with the same tool ID, snapshot hash, and source commit;
5. an `evidence_returned` event containing that tool for the consumer;
6. an exact hash-checked copy receipt;
7. a later attributed verifier consuming that receipt;
8. an objective adoption View and correct confounding classification;
9. a selected/promoted artifact that is independent of the temporary inbox and
   run shared directory;
10. monitor funnel counts matching candidate iteration records and the raw
    trace.

### Exit Gate

Layer 1 passes only when the complete chain succeeds in three consecutive clean
primary-host runs and the monitor contains no malformed trace lines. A failure
permits fixes to instrumentation or an already-specified mechanism defect, but
does not justify changing adoption policy. Restart the consecutive-run count
after a mechanism fix.

## Layer 2: Natural Opportunity Observation

### Purpose

Measure where ordinary autonomous runs lose shared-dir opportunities without
forcing publisher/consumer roles or requiring adoption.

### Case Selection

Create a locked pilot manifest before running. Use at least 12 cases across at
least three long-horizon task families, with two repetitions per case. Include:

- repository debugging or repair tasks with reusable reproduction/probe work;
- optimization tasks with repeated profiling, parsing, trace, or correctness
  checks;
- at least one low-shareability stratum as a negative control.

Tag each case before results are visible:

- `high`: two or more lanes are expected to rebuild the same non-trivial probe,
  parser, trace, or setup;
- `medium`: reuse is plausible but depends on the discovered direction;
- `low`: candidate work is mostly independent or the task is too short for a
  second peer iteration.

#### Locked Pilot Manifest

Use the following 12-case manifest for the first full Layer 2 campaign. The two
benchmark groups have different observational roles and must be analyzed as
separate strata rather than pooled into one score distribution.

EdgeBench supplies the continuous-score stratum. The `2h -> 12h` values below
are the official GPT-5.5 reference curve, used only as task metadata to select
cases with different amounts of optimization headroom. They are not expected
scores for this campaign and must not be used to select candidates within a
run. The source is the
[official EdgeBench task table](https://github.com/ByteDance-Seed/EdgeBench)
and the checked-in
[reference projection](../bench-goal-plus/experiments/edgebench/references/paper-gpt-5.5-codex-12h.json).

| Case | Family | Shareability | Expected reusable tool work | Selection role |
| --- | --- | --- | --- | --- |
| `vliw_kernel_optimization` | systems / instruction scheduling | `high` | cycle breakdown, dependency analysis, and instruction-trace summaries | Calibrated first case with an existing Goal Plus `K=2` path; reference `71.6 -> 85.6`. |
| `ad_placement_optimization` | combinatorial optimization | `high` | constraint checks, batch scoring, and move-delta analysis | Clear iterative search surface and substantial reference headroom, `44.0 -> 62.9`. |
| `integer_compression_codec` | systems / codec engineering | `high` | round-trip fuzzing, corpus benchmarks, and bit-rate profiling | Separates correctness tooling from performance tooling; reference `61.1 -> 74.4`. |
| `borden_source_inversion` | scientific / numerical inversion | `high` | residual decomposition, parameter sweeps, and regularization comparisons | Sustained long-horizon improvement rather than an early pass/fail transition; reference `20.1 -> 38.5`. |
| `new_foundations_consistency` | formal reasoning | `high` | compile-error clustering, lemma dependency queries, and proof-hole batch checks | Tests whether diagnostic transfer changes a difficult proof trajectory; reference `13.7 -> 66.5`. |
| `graph_node_classification` | scientific ML / feature engineering | `medium` | feature ablation, cross-validation, and experiment aggregation | Low score-headroom control: tool sharing may occur even when the reference curve is nearly flat, `54.7 -> 56.0`. |

SWE-bench Verified supplies the repository-debugging stratum. Select cases only
from the frozen dataset revision already used by the benchmark controller. The
expected tool work below is a predeclared hypothesis based on public issue
metadata; it is not a worker role assignment.

| Case | Public issue family | Shareability | Expected reusable tool work | Selection role |
| --- | --- | --- | --- | --- |
| `django__django-15957` | ORM sliced prefetch, `1-4 hours` | `high` | SQL/queryset traces, backend behavior matrices, and minimal model generators | Multi-layer framework behavior with several plausible diagnostic directions. |
| `pytest-dev__pytest-5787` | distributed exception serialization, `1-4 hours` | `high` | local/xdist output comparison and exception-chain dumpers | A compact reproducer can transfer independently of the eventual serialization fix. |
| `sphinx-doc__sphinx-11510` | parser and event pipeline, `1-4 hours` | `high` | minimal documentation project generation, event traces, and HTML assertions | Separates pipeline observation tooling from implementation changes. |
| `scikit-learn__scikit-learn-25102` | DataFrame dtype propagation, `1-4 hours` | `high` | dtype/transformer matrices and regression-result aggregation | Broad input combinations create repeated probe work across candidates. |
| `astropy__astropy-12907` | nested model separability, `15 min-1 hour` | `medium` | compound-model generation and expected-matrix diffs | Faster feedback case with a structured but non-trivial reproducer. |
| `sympy__sympy-16886` | incorrect Morse mapping, `<15 min` | `low` | basic location and focused-test commands | Deliberate low-opportunity control; exclude it from positive utility estimates if it resolves before peer visibility. |

The current SWE-bench native controller accepts only `K=1`, requires the
evidence annotator to be disabled, and maps a selected public command to a
binary worker-visible `0/1` score. Therefore SWE-bench cells are not valid
Layer 2 shared-dir observations until the benchmark adapter has a tested
`K>=2` topology and Tool View-capable evidence annotation while preserving the
same hidden-evaluator boundary. These are experiment-admission requirements,
not reasons to change shared-dir semantics. See
[`config.py`](../bench-goal-plus/experiments/swe_bench_verified/config.py) and
[`visible_test_verifier.py`](../bench-goal-plus/experiments/swe_bench_verified/visible_test_verifier.py).

Run the manifest in this order:

1. Calibrate trace collection on `vliw_kernel_optimization`.
2. Run the other five EdgeBench cases after their exact task assets pass local
   inventory and doctor checks.
3. Admit the six SWE-bench cases only after the `K>=2` controller and evidence
   annotation path passes a real shared-dir smoke. Run the three
   high-opportunity pipeline/framework cases first, then the scientific cases
   and the low-opportunity SymPy control.

Do not select cases because a previous treatment run scored well. When an
existing comparison campaign has a checked-in identity manifest, reuse it
exactly.

### Execution

Enable shared-dir and trace. Keep normal worker instructions and initial
planning behavior; do not assign dedicated publishers or adopters. Hold host,
model, budget, candidate count, verifier contract, source revision, and seed
policy fixed across repetitions. Allow workers to reject irrelevant tools.

Capture a monitor snapshot periodically and once after all workers are quiet.
Generate the normal report only after selection, promotion/result recording,
and terminal Goal Plus status.

### Primary Outputs

Report by task family and shareability stratum:

- the complete `S -> P -> V -> O -> C -> R -> I -> G` funnel;
- the four trace latency distributions;
- tools and families per run, publication errors, annotation terminal errors,
  and toolization exclusions;
- peer verifier opportunities after first visibility, derived by comparing the
  trace with candidate iteration timestamps;
- adoption disposition and score delta only when a prior candidate baseline
  exists;
- time, token, cost, verifier, and annotation overhead;
- final benchmark result, but without attributing it causally to shared-dir.

For EdgeBench, report the native normalized score at each available verifier
checkpoint and candidate score deltas after isolated adoption. For SWE-bench,
report the funnel, time to the first passing worker-selected public verifier,
and the final hidden official `resolved` value only after workers are final.
Do not expose official test identities or derive a worker-visible continuous
score from hidden evaluation data.

For `visible but not copied`, the first campaign records only the objective
fact. Do not infer worker intent from absence of a copy. If this class dominates
after Layer 2, a separate temporary structured review experiment may be
designed; it is not part of the initial instrumentation.

### Exit Gate

Layer 2 is interpretable when all of the following hold:

- at least 24 valid runs are present from the locked manifest;
- at least 10 unique peer visibility pairs exist overall;
- at least 5 visibility pairs exist in the high-shareability stratum;
- infrastructure failures are below 10%, or are separately explained and
  rerun under the retry rule;
- raw trace, monitor funnel, and candidate records agree on every sampled path.

If fewer than 10 visibility pairs exist, report the dominant upstream loss
class and stop. There is not yet enough evidence to claim that adoption lacks
utility. This gate is the earliest point at which shared-dir semantic changes
may be proposed.

## Layer 3: Paired Causal Effect

### Purpose

Estimate end-to-end benefit and overhead after the opportunity funnel is known.

### Design

Use a fresh locked holdout manifest. Select 8-12 cases using only task metadata
and the predeclared shareability rubric, not Layer 2 treatment scores. Run at
least three paired repetitions per case:

- control: `shared_dir.enabled=false`;
- treatment: `shared_dir.enabled=true` with trace enabled.

Within every pair keep case identity, source commit, verifier artifacts, host,
exact model, candidate count, initial proposal policy, worker budget, timeout,
and seed identical. Randomize treatment order within each case/seed block.
Do not reuse candidate workspaces or native sessions across arms.

The two-arm comparison estimates total product effect, including toolization
and annotation overhead. Do not add a shadow-discovery arm until the initial
campaign is complete because suppressing visibility would itself add a new
shared semantic mode.

### Outcomes

Predeclare one benchmark-native primary outcome per task family. Normalize only
with a formula fixed before running. Secondary outcomes are:

- pass/success rate;
- best valid score and target attainment;
- time and verifier count to first improvement and final best;
- worker plus annotator tokens, cost, and duration;
- treatment funnel and conditional isolated-adoption outcomes.

Analyze paired differences by case and seed. Report every pair, mean and median
paired difference, direction consistency, and a case-clustered bootstrap 95%
interval. With a small pilot, treat the interval as uncertainty description,
not a binary significance gate. Report control/treatment infrastructure
failures separately and apply the same retry rule to both arms.

### Decision Rules

- Positive end-to-end result: treatment improves the primary paired outcome
  without unacceptable correctness or cost regression, and the effect is not
  confined to one case.
- Useful but opportunity-limited: isolated adoptions often improve, but `O` or
  `C/O` is low and total paired effect is neutral.
- Overhead-limited: conditional utility exists, but treatment time/token cost
  removes the gain.
- No demonstrated utility: sufficient `I` exists, yet `G/I` and paired outcomes
  remain neutral or negative across strata.
- Inconclusive: opportunity or valid pair counts do not meet the gates.

Only after classifying the result should the team choose a semantic change.
Possible directions include publication prompting, faster or structural Tool
Views, evidence refresh timing, relevance presentation, copy ergonomics, or
adoption outcome evidence. Change one dominant loss class at a time and rerun
the corresponding layer before returning to the full paired campaign.

## Hidden-answer Benchmarks

For MMLU, ARC, WinoGrande, TruthfulQA, GSM8K, or any equivalent hidden-answer
dataset, worker-visible verification may check format only. Keep gold labels,
correctness, scores, predictions, and scoring commands outside frozen specs,
worker context, candidate workspaces, shared tools, and Tool Views. Finalize all
candidate answers first, aggregate with a predeclared gold-independent rule,
then score in the parent evaluator. Reuse the checked-in case identity manifest
and verify benchmark, case index, and question hash. Do not use web or local
answer lookup during answering runs.

Normal verifier-guided Search and hidden-answer QA measure different things;
never merge their scores into one effectiveness claim.

## Campaign Deliverables

Publish the following after each layer:

- frozen case/run manifest and environment matrix;
- per-run IDs and terminal status;
- funnel table and latency table;
- infrastructure failure ledger;
- paired outcome table where applicable;
- a short conclusion limited to the layer's allowed claim;
- the next decision gate, without bundling multiple semantic changes.

Raw `.gp`, host transcripts, credentials, and hidden answers remain local and
ignored. Export only content-safe derived evidence to the benchmark evidence
repository.
