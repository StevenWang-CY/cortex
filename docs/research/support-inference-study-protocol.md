# Preregistered study protocol: workspace-support inference

**Status:** protocol template; no study has yet established product validity

**Decision target:** whether a specific, reversible workspace-support proposal
would be helpful at a specific eligible decision point. This protocol does not
label or diagnose an internal cognitive, emotional, neurological, or medical
state.

## 1. Research questions

1. Can pre-decision interaction aggregates identify decision points where a
   user reports that a specific support proposal would be helpful, with useful
   selective performance on entirely held-out participants?
2. Conditional on eligibility and availability, does presenting that proposal
   improve a preregistered proximal outcome compared with no proposal?
3. How do missingness, abstention, calibration, proposal burden, and effect vary
   across supported devices, task classes, input modes, and participant groups?

Question 1 is predictive. Question 2 is causal and requires randomized action
assignment. Predictive performance or post-intervention feedback alone cannot
answer Question 2.

## 2. Governance and preregistration

Before recruitment, register the protocol, hypotheses, primary endpoint,
decision-point eligibility, sample-size calculation, exclusion rules, feature
catalog/version, analysis code hash, split seed, model families, tuning budget,
calibration method, subgroup plan, multiplicity handling, and release gates.
Obtain the required ethics/IRB determination and explicit informed consent.
Participants must be able to pause capture, inspect collected fields, withdraw,
and request deletion without losing ordinary product functionality.

Report development and evaluation transparently using the principles and
checklist in [TRIPOD+AI](https://www.bmj.com/content/385/bmj-2023-078378),
including participant flow, missing data, full model specification, evaluation
data distinction, subgroup results, protocol deviations, and open-science
artifacts. Use [NIST AI RMF 1.0](https://doi.org/10.6028/NIST.AI.100-1) to
document intended context, measurement validity, risks, TEVV, monitoring,
incident response, and rollback. These references constrain reporting and risk
management; they do not make Cortex a clinical system.

## 3. Population and sampling

Define the intended deployment population and supported hardware before power
analysis. Recruit across operating-system versions, camera/input hardware,
workflows, experience levels, accessibility needs, skin tones if any camera
metric is separately studied, lighting conditions, and task categories.
Record recruitment source and non-participation. Avoid a convenience sample of
only project developers or one institution.

Participant is the unit of independence. Multiple episodes from one person are
correlated and must never be split across development and evaluation folds.
Prespecify minimum participants, episodes per participant, maximum participant
weight, and attrition allowance using simulation under plausible prevalence,
within-person correlation, abstention, and effect sizes. Do not choose sample
size from a generic events-per-variable rule after observing results.

## 4. Decision points, labels, and exclusions

An episode begins at a preregistered eligible decision point and includes only
features available strictly before randomization/presentation. Each episode has
a stable participant ID, session ID, episode ID, timestamps, feature-schema
version, algorithm identities, availability mask, quality, proposal identity,
randomization probability, presentation result, explicit response, and proximal
outcome window.

The predictive label is proposal-specific:

- `support_helpful`: the participant says the named proposal would be helpful
  at that decision point using the preregistered instrument;
- `support_not_helpful`: the participant says it would not be helpful;
- `uncertain`: ambiguous, missing, or declined response; excluded from binary
  model fitting but reported in label availability and sensitivity analyses.

Dismissal, acceptance, dwell time, task completion, and intervention reward are
outcomes or behavior, not ground-truth cognitive-state labels. Never convert
them into `HYPER`, `FLOW`, stress, or overwhelm labels. Collect label before
showing the randomized proposal when measuring perceived need; collect outcome
afterward when measuring effect. Where burden permits, repeat a subset with
short delay to estimate label stability.

Predeclare exclusions and apply them blind to outcome:

- withdrawn/absent consent;
- corrupt, impossible, or wrong-clock telemetry;
- duplicate episode;
- protocol deviation that changes feature availability or label timing;
- decision point outside the registered availability/receptivity rules.

Do not exclude difficult participants, negative outcomes, missing camera data,
or low model scores after looking at performance. Report every exclusion and
reason. Keep `uncertain` and missing labels in flow diagrams and sensitivity
analyses.

## 5. Feature and measurement plan

Freeze `support-features-v2.1.0` before the first confirmatory participant. For
every feature, publish unit, source, aggregation window, minimum observation
count/exposure, transform, valid range, quality algorithm, age limit, missing
reasons, and code digest. Verify raw-event-to-aggregate calculations with
synthetic and replay fixtures.

Primary production-candidate inputs are behavior aggregates only. Camera pulse,
blink, and head/neck proxies remain diagnostic-only and excluded from the
primary predictive model. A separate measurement study must compare any camera
metric against an appropriate synchronized reference across participants,
devices, lighting, motion, face visibility, and missingness. Passing a signal
measurement gate still would not prove that the signal predicts support need.

No raw frames, key contents, document contents, URLs, or window titles enter the
inference dataset. Store only consented aggregates necessary for the protocol,
with retention, encryption, access, deletion, and breach procedures specified.

## 6. Split and model-development plan

Run all data checks and splits at participant level. The executable scaffolding
is `evaluation_protocol.py`.

1. Freeze a final participant-held-out evaluation set before model selection.
2. Within the remaining development participants, use grouped nested
   cross-validation for preprocessing, feature selection, hyperparameters, and
   threshold selection.
3. Reserve whole development participants for probability calibration. Fit a
   calibrator only after the underlying model is frozen; never calibrate and
   evaluate on the same episodes or participants.
4. Keep the final evaluation set sealed until the pipeline, model card, missing
   data plan, and analysis script are frozen.
5. If data come from multiple sites, applications, or time periods, include an
   external/temporal validation set rather than relying only on random grouped
   folds.

All preprocessing is fitted inside each training fold. Baselines, imputation,
normalization, feature selection, and calibration must not see test
participants. Missingness indicators remain explicit. Compare against simple
prespecified baselines: always abstain, prevalence-only, the deterministic
rules, and a small regularized model. Complexity must earn its inclusion.

The existing `ml_classifier.py` is research-only. It cannot be enabled merely
because it trains successfully; a production candidate needs a versioned model
artifact, feature-schema digest, immutable model card, evaluation report,
separate calibration artifact, registry entry, and safe rollback.

## 7. Predictive estimands and metrics

Primary predictive estimand and horizon must be preregistered. Report at least:

- prevalence and label-availability rate;
- participant-held-out AUROC and, because class imbalance is likely, AUPRC;
- sensitivity, specificity, PPV, NPV, and confusion matrix at each frozen
  operating threshold;
- Brier score, calibration intercept/slope, and reliability plots for any
  claimed probability;
- selective risk versus coverage, abstention rate, and missingness-conditioned
  performance;
- participant-level bootstrap confidence intervals or a prespecified clustered
  analysis;
- decision-curve/net-benefit analysis using explicit costs of false proposals,
  missed support, and interruption;
- performance and coverage for preregistered task, device, input-mode,
  accessibility, and demographic subgroups where sample size and consent allow.

Do not call softmax output, normalized rule scores, evidence strength, or
coverage a probability. Do not choose thresholds on the final evaluation set.
Report distributions and uncertainty, not only point estimates. Investigate
worst-group behavior and abstention burden; overall averages cannot excuse a
material subgroup failure.

## 8. Causal intervention evaluation

To estimate whether a proposal helps, run a separately consented
micro-randomized trial at eligible decision points. Randomize among a named
proposal, a minimal alternative, and no proposal with logged probabilities.
The design and analysis must account for repeated decisions, availability,
time-varying context, burden, and delayed effects. The foundational
[micro-randomized trial paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC4732571/)
describes why repeated randomization supports estimation of proximal causal
effects in just-in-time interventions.

Predefine:

- availability and safety exclusions before randomization;
- intervention components and exact presentation;
- primary proximal outcome and observation window;
- burden/adverse outcomes, dismissals, undo, and task disruption;
- excursion-effect estimator, moderators, randomization probabilities, and
  missing-outcome method;
- limits on prompts per participant and stopping/incident rules.

Do not use ordinary logged bandit rewards as causal evidence when action
propensities, no-action outcomes, or repeated updates are missing. Do not update
the production policy online during the confirmatory trial unless that adaptive
design and its inference are themselves preregistered.

## 9. Release gates

Before data collection, owners must set quantitative minimums from the intended
use, prevalence, error costs, and sample-size simulation. At minimum, promotion
requires all of the following, with confidence intervals:

- materially better participant-held-out utility than the deterministic and
  prevalence baselines at the intended operating point;
- acceptable calibration on a separately calibrated, sealed evaluation set if
  probabilities are exposed;
- a useful risk/coverage tradeoff with explicit abstention;
- no prespecified subgroup or supported input mode crossing its unacceptable
  harm/performance boundary;
- stable results in an external or temporal validation cohort;
- demonstrated proposal benefit and acceptable burden in the randomized study;
- privacy, security, accessibility, UX, schema, monitoring, rollback, and
  incident-response review complete.

Failure means remain on deterministic rules or activate safety-null. It does not
justify relabeling scores, weakening exclusions, performing a new unregistered
analysis, or silently narrowing the claimed population.

## 10. Reproducibility package

Archive the preregistration, protocol amendments, consent/ethics determination,
data dictionary, participant-flow table, feature catalog/digest, immutable split
manifest, environment lockfiles, model/preprocessing/calibration artifacts,
source commit, analysis scripts, seeds, evaluation output, subgroup tables,
model card, known-failure cases, and signed release decision. Publish code and
appropriately de-identified aggregate artifacts where consent and privacy allow;
document why any artifact cannot be shared.

