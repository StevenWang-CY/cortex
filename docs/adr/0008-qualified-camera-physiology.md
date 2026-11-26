# ADR 0008: Qualified camera physiology corroborates the support rules

- Status: Accepted
- Date: 2026-11-25
- Supersedes: the clause of
  [ADR 0001](0001-conservative-support-inference.md) that keeps camera pulse,
  HRV, respiration and blink diagnostic-only. The rest of ADR 0001 — the claim
  boundary, abstention, deterministic rules, suggest-only default — stands.

## Context

ADR 0001 excluded every camera-derived value from production rules "until
metric-specific reference and participant-held-out gates pass". No such gate
was ever run, so the exclusion was permanent in practice: the app measured a
pulse, displayed it, and ignored it. The behaviour-only rules cannot see what a
person's body is doing while their hands are still, and a break reminder on a
fixed timer fires regardless of how the preceding hour went.

Running the 0.5.1 estimator against a public ECG-referenced video set showed
why it could not have passed a gate as it stood: it published about 8 % of
windows, its signal-presence test did not distinguish right rates from wrong
ones, and it could never publish a breathing rate. The estimator was rebuilt
and re-measured; the method, figures and limits are in the
[qualification report](../research/physiology-qualification.md).

The evidence is real and narrow. Heart rate and SDNN agree with ECG (mean
errors 1.8 BPM and 8.2 ms) on one participant. On two participants with
intermediate and dark skin tone the estimator abstained on every window,
including bright-light windows of one of them in which a mostly correct pulse
was present. RMSSD does not agree. Breathing rate has only simulated truth
behind it, and blink rate and eye-closure share were compared with nothing.
There is no held-out data, and nothing connects any of these values to a need
for support beyond the direction of group-level findings.

"Qualified" in the title covers heart rate and SDNN only: the two metrics that
were compared with a reference sensor and met their criteria. The other
admitted inputs are admitted on weaker grounds, stated in the table.

## Decision

Camera physiology is admitted to the production rules as **corroborating
evidence with bounded influence**, metric by metric, according to what each one
showed:

| Metric | Decision |
| --- | --- |
| Heart rate, SDNN | Production inputs, qualified against ECG; carry most of the support-likely and flow-like physiology blocks |
| Breathing rate | Production input without a reference check, at the lowest weight of the cardiac inputs; published only when head movement and a pulse-derived channel agree |
| Blink rate, eye-closure share | Production inputs without any check; read only while heart rate or SDNN is usable in the same evaluation. Eye-closure share is 40 % of the under-engaged block |
| RMSSD, pNN50, SD1/SD2, LF/HF, sample entropy | Not admitted; computed for research only |
| Head/neck flexion | Unchanged: a comfort proxy, not an input |

Everything downstream that used to be handed RMSSD — the session report, the
multi-day trend and the coding-practice struggle detector — now receives SDNN
or nothing. Two coding-practice modes that physiology would establish by
itself (an immediate heart-rate reaction to a submission, and a post-success
recovery window) stay contained: their physiological inputs remain zero, and
the coach's mode resolver is given no strain load.

The bounds that make this acceptable on narrow evidence are structural, not
statistical:

1. **Abstention first.** A value is published only when its own gate passes.
   Unpublished is the normal outcome under movement, low light or a weak
   signal, and it leaves the rules exactly as they were without a camera.
2. **Personal reference.** Heart rate, SDNN, breathing rate and blink rate are
   read as deviations from the same person through the same camera, never
   against a population norm. Eye-closure share is the exception: it is read
   as an absolute fraction.
3. **Corroboration, not establishment.** Physiology is mixed into a state's
   score only where that state's behaviour rule is itself eligible, with a
   weight capped below the smoother's entry threshold. It may lower any
   eligible score, and may raise one only when the behaviour score alone
   already reaches the entry threshold. A weak or ambiguous behavioural
   reading therefore cannot become an estimate because of the camera. Between
   hypotheses that behaviour has each carried past the threshold, physiology
   can decide which leads.
4. **Suggestions stay suggestions.** The one new action — a breathing-break
   suggestion when a strain load crosses its threshold while camera readings
   are still arriving — passes the same interruption gate as every other
   suggestion, says only that camera readings have stayed away from the
   person's usual level, and changes nothing in the workspace.
5. **One switch each.** `state.physiology_weight: 0` restores behaviour-only
   scoring; `intervention.enable_strain_break_suggestions: false` removes the
   suggestion.

The claim boundary of ADR 0001 is unchanged. Outputs remain support hypotheses
and wellness estimates. They are not diagnoses, probabilities or measurements
of stress, and product copy must keep saying so.

## Consequences

- Users for whom the camera yields a pulse get rules with a second line of
  evidence beside behaviour, and break suggestions tied to how the session
  has gone instead of to a timer alone. Whether that evidence separates a
  calm stretch from a strained one in practice has not been tested.
- Users for whom it does not — on current evidence that includes people with
  darker skin on comparable footage, and anyone in poor light or frequent
  motion — get the 0.5.1 behaviour-only rules and no physiology-based
  suggestion. This is an unequal benefit. It is preferred to publishing a wrong
  number, and it is a known gap to close with better evidence, not a settled
  state.
- A metric's admission is tied to its qualification. A change to an estimator,
  gate or window requires the qualification to be re-run and the report and
  model card updated in the same change. The synthetic run, including zero
  false publication on signal-free input, is a step of `make verify`
  (`make qualify-physiology`) and fails when an admitted metric misses its
  criterion. Hosted CI does not run it.
- Admission can be withdrawn per metric by setting its weight to zero in a new
  model version if later evidence contradicts this report.
- What ADR 0001 asked for and this decision does not deliver remains owed:
  participant-held-out evaluation, more people across skin tones and cameras,
  a reference respiration sensor, and the prospective study that would show
  whether a fused score predicts a need for support.

Evidence: [qualification report](../research/physiology-qualification.md),
[model card v3](../model-cards/deterministic-support-v3.md).
