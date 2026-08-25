# Policy and evaluation protocol

Status: implementation contract for policy lifecycle v2. This is not a claim
that Cortex improves cognition, health, productivity, or any distal outcome.

## Evidence boundary

Cortex has two deliberately separate policy modes:

| Mode | Purpose | Assignment | May support causal/OPE analysis? |
|---|---|---|---|
| `deterministic` | shipping product behavior | versioned ordered rules | no |
| `research_randomized` | separately consented, fixed-epoch MRT | reproducible masked randomization | only after all gates in this document pass |

Deterministic product records never contain a propensity distribution and set
`supports_ope=false`. A selected probability of `1.0` records certainty of a
rule result; it is not a behavior propensity. Descriptive reports are named
`policy_diagnostics_YYYY-MM-DD.md` and explicitly make no effect claim.

The old AMIP, LinUCB, and `causal_*.md` artifacts are
`legacy_diagnostic_only`. Their missing decision points, availability,
control follow-up, assignment probabilities, delivery state, and single
reward finalization cannot be reconstructed after the fact.

## Production policy

`cortex-production-ordered-rules/2.0.0` is deterministic and has no online
update path. For each durable decision point it applies these rules in order:

1. choose `no_action` when ineligible or unavailable;
2. choose `no_action` after the repeated-dismissal safety gate;
3. choose the configured low-friction arm when it is feasible;
4. otherwise choose `suggest_only` when feasible;
5. otherwise choose `no_action`.

The decision record freezes the policy/version/state digest, context schema,
eligibility, availability and reason, ordered feasible arms, selected arm,
reward version, session, decision-point UUID, wall time, monotonic time, and
boot identity before any proposal is created. Independent replays over an
identical input must agree on the selected arm and state checksum. CI measures
the mismatch rate and requires exactly zero.

## One lifecycle for action and no action

Every v2 decision receives the same transactional lifecycle:

```text
decision + initial snapshot + scheduled window
                    |
          one delivery disposition
                    |
     zero or more idempotent observations
                    |
      finalized or censored outcome window
                    |
        exactly one reward/version
```

The decision and its pending window are committed together. `no_action` gets
`not_applicable`; an active arm gets `delivered` only after an authenticated UI
surface receives it. Missing active delivery is finalized as `not_delivered`.
Duplicate delivery, observation, finalization, or decision-point writes either
return the identical result or fail on a content conflict.

The proximal window is based on a fixed close time recorded at assignment. It
does not close early on rating, dismissal, undo, or recovery. Restart recovery
queries the same pending SQLite rows. Both action and no-action decisions
receive a post-window snapshot. Missing snapshots are marked `censored`, not
silently imputed.

Contamination records every other policy delivery whose assignment time falls
inside the window plus any explicit collector-supplied cause. The assigned
intervention itself remaining visible at window close is not misclassified as
contamination. The primary MRT analysis excludes contaminated and censored
windows but retains them in the immutable export with an exclusion reason.

## `helpfulness-v2` proximal outcome

The reward is bounded to `[-1, 1]` and finalized once:

```text
R = 0.45 rating
  + 0.25 terminal user action
  + 0.15 task measure
  + 0.10 restore-failure penalty
  + 0.03 delivery-failure penalty
  + 0.02 interruption penalty
```

Component encodings are part of the persisted reward record:

- rating: thumbs up `+1`, thumbs down `-1`, absent `0`;
- action: engaged/natural recovery `+0.5`, dismissed `-0.75`, snoozed
  `-0.4`, restore/system cancellation/absent `0`; undo caps this component at
  `-0.75`;
- task measure: bounded combination of reduced workspace complexity (60%) and
  reduced error count (40%);
- restore failure, delivery failure, and delivered interruption contribute
  `-1` to their respective weighted components.

The task term intentionally does not reuse the support score that triggered a
decision. That avoids directly rewarding the system for changing its own
trigger variable. It is still only a proximal, constructed outcome. Its
content validity and sensitivity require independent study; coefficient
choices must not be tuned after looking at an epoch's treatment comparison.

The older immediate `HelpfulnessTracker` remains a descriptive product UI
summary. It does not update either v2 policy and cannot enter the research
analysis.

## First supported micro-randomized trial

The first research epoch is intentionally narrow:

- action catalog: ordered pair `no_action`, `suggest_only`;
- decision point: a reviewed trigger opportunity after the production
  eligibility gate and decision cadence;
- availability: both arms are feasible, receptivity passes, repeated-dismissal
  suppression is inactive, and no other safety exclusion applies;
- assignment: fixed two-arm probabilities (currently 0.5/0.5 because online
  learning is forbidden), with a configured positive-probability floor;
- proximal outcome: one frozen `helpfulness-v2` window;
- primary estimand: marginal proximal effect of `suggest_only` versus
  `no_action` among available decision points in the enrolled epoch;
- cluster: session identity;
- primary missingness rule: retain the row, mark it censored, and exclude it
  from complete-case primary analysis;
- primary contamination rule: retain the row with causes and exclude the
  contaminated window.

Research mode is invalid unless `eval.research.enabled=true` and study ID,
epoch, separate consent version, 32-byte lowercase seed, fixed action catalog,
analysis seed, bootstrap count, and all four protocol rules are present.
Safety exclusions are logged as deterministic no-action records and cannot
enter the randomized export.

The full `MRTStudySpecification` is constructed at daemon startup. Its
canonical SHA-256 is embedded in the research policy's checksummed state and
in every randomized decision. Export refuses a row whose digest differs from
the requested specification. Changing any study identity, rule, outcome
window, estimator seed, or action definition therefore requires a new epoch.

### Reproducible assignment

For counter `c` at decision-point UUID `d`, assignment derives a draw from:

```text
HMAC-SHA256(seed, study_id | epoch | d | c)
```

The first 64 digest bits map to `[0,1)`. The record stores seed, counter,
decision-point UUID, assignment UUID, exact propensity vector, selected
probability, and the post-increment policy-state checksum. Reloading the
checksummed state must reproduce the next assignment exactly. Duplicate
assignment IDs or `(seed, counter)` pairs make export fail.

The seed is research-sensitive local data. Exports are owner-readable files;
sharing them is a separate research-data disclosure decision.

## Primary MRT analysis

The export is canonical JSON with a SHA-256 sidecar and a non-overwriting UUID
filename. It contains the frozen specification, every matching randomized
decision, delivery, outcome status, contamination, reward components, and an
explicit inclusion flag. It excludes raw frames, waveform samples, source
code, terminal text, and full URLs.

Analysis verifies both checksums, exact study/policy/consent/reward identity,
ordered action support, positive propensities, selected probability, unique
draw identity, and exact window length. The primary implementation uses
weighted and centered least squares (WCLS):

```text
X_t = [1, A_t - p_t]
w_t = 1 / (p_t (1 - p_t))
beta = argmin sum_t w_t (Y_t - X_t beta)^2
```

The reported effect is the coefficient on `A_t - p_t`. Uncertainty includes a
session-cluster sandwich standard error and a seeded session-cluster bootstrap
percentile interval. The report also includes included/excluded points,
cluster count, propensity range, effective sample size, completed bootstrap
replicates, source export digest, and an interpretation boundary.

Before any external causal language, the protocol, sample size/power,
availability definition, proximal-outcome validity, missing-data strategy,
contamination strategy, moderation terms, finite-sample correction, and
analysis code require independent statistical and ethics review. One local
report is not that review.

## Off-policy evaluation is sensitivity analysis

The OPE module accepts only records that explicitly support OPE and a named,
versioned target policy with an exact action catalog and probability rule. It
rejects deterministic logs, duplicate decision IDs, empty cluster IDs,
incomplete distributions, non-finite/unbounded outcomes or models, observed
actions with zero behavior probability, and target mass outside behavior
support.

It reports direct method, IPS, SNIPS, doubly robust, clipped DR, and SWITCH-DR
alongside overlap, effective sample size/fraction, behavior/target minimum
probability, maximum/p95/p99 importance weights, counts beyond clip/switch
thresholds, estimator range, and seeded cluster-bootstrap intervals. The
result hashes both the exact per-row target assignments and every evaluation
input row. This prevents a prose target-policy label from obscuring the actual
probabilities evaluated.

Estimator agreement is a sensitivity diagnostic, not proof. OPE results must
not be used to promote a target policy when overlap is weak, effective sample
size is inadequate, tail weights are extreme, the outcome model was selected
post hoc, or the logged epoch differs from the target's population or action
semantics.

## Operating procedure

1. Obtain separate research/ethics approval and informed consent outside the
   product consent ladder.
2. Freeze all `eval.research` fields and `eval.outcome` fields. Start a new
   `study_epoch` for any change.
3. Archive the reviewed config digest before enrollment.
4. Run deterministic replay, schema drift, migration, lifecycle, and synthetic
   known-effect gates.
5. Collect data without changing reward coefficients, action definitions,
   assignment probabilities, availability, or primary exclusions.
6. Request a local export through authenticated `POST /research/mrt/export`
   with the literal confirmation `EXPORT CONSENTED RESEARCH DATA` and the exact
   generated specification.
7. Preserve the JSON and `.sha256` sidecar together. Analyze only a verified
   copy with authenticated `POST /research/mrt/analyze` or
   `analyze_mrt_export()`.
8. Report exclusions, missingness, contamination, failed deliveries,
   propensity diagnostics, clusters, and all prespecified uncertainty—not only
   a point estimate.

Operational counts can be generated separately with authenticated
`POST /policy/diagnostics`. Those reports must never be renamed to imply an
effect estimate.

## Automated gates

The test suite covers:

- fresh schema and transactional v1→v2 migration with verified backup;
- deterministic product replay and absence of propensity claims;
- identical action/no-action window handling and exactly one reward/version;
- idempotent feedback, late-feedback rejection, restart recovery, missing
  delivery, and contamination;
- research feasibility masks, hard exclusions, positive propensities, seeded
  replay, state reload, state/specification checksum corruption;
- immutable exports, exact window/specification binding, and legacy report
  naming;
- synthetic WCLS recovery with session-cluster bootstrap;
- known-value OPE, deterministic-log refusal, support failure, and extreme
  weight diagnostics;
- CI regression baseline for trigger behavior and zero deterministic replay
  mismatches.

## Research basis

- Klasnja et al., [Micro-Randomized Trials: An Experimental Design for
  Developing Just-in-Time Adaptive Interventions](https://pmc.ncbi.nlm.nih.gov/articles/PMC4732571/),
  defines proximal effects among available decision points and motivates
  repeated randomization.
- Liao et al., [Practical Considerations for Data Collection and Management in
  Mobile Health Micro-randomized Trials](https://pmc.ncbi.nlm.nih.gov/articles/PMC6713230/),
  motivates protocolized availability, exact decision-point collection, and
  delivery/context integrity.
- Klasnja et al., [Efficacy of Contextually Tailored Suggestions for Physical
  Activity](https://pmc.ncbi.nlm.nih.gov/articles/PMC6401341/), reports the
  HeartSteps centered/weighted analysis and explicitly limits the target to a
  proximal intervention-component effect rather than whole-system efficacy.
- Dudík et al., [Doubly Robust Policy Evaluation and
  Learning](https://arxiv.org/abs/1103.4601), motivates combining a reward
  model with propensity weighting while retaining both sets of assumptions.
- Wang, Agarwal, and Dudík, [Optimal and Adaptive Off-policy Evaluation in
  Contextual Bandits](https://proceedings.mlr.press/v70/wang17a.html), motivates
  estimator/weight tradeoffs including SWITCH-style diagnostics.
- Kuzborskij et al., [Confident Off-Policy Evaluation and Selection through
  Self-Normalized Importance Weighting](https://proceedings.mlr.press/v130/kuzborskij21a.html),
  reinforces that self-normalization and uncertainty are central to policy
  evaluation rather than optional reporting details.
