# Metrics Glossary

Single source of truth for every derived quantity GUV-CFD computes and
reports. **Every entry below leads with the exact text that appears on
screen** — in the GUI Analysis tab and in the exported `.docx` report —
because that's what a user actually sees; the internal code field name is
listed too, but only as a secondary reference for developers, never as a
substitute for the on-screen label. Where GUI and docx disagree, or where
a field is missing from one of them, that's called out explicitly.

Built from a full audit of `app.py` and `report.py` (2026-07-27).

## How to read each entry

```
### "Exact text shown on screen"
- Where shown: GUI Analysis tab / docx report table / both / neither
- Code field: `results.json` key(s) behind this number
- Meaning: what it actually measures, in plain words
- Formula: the exact calculation
- Type: [air] pure flow-rate, never touches contaminant field
         [T]   derived from the contaminant field's own behavior
- Issues: any mismatch, ambiguity, or missing counterpart
```

**Air vs. T reminder**: [air] is a pure flow-rate measurement (velocity ×
area) — nominal ACH, CFD-delivered ACH. [T] is derived from the
contaminant field `T`'s own behavior — a decay-curve fit or a
steady-state mass-balance ratio. Confusing the two is the single most
common source of mislabeled results in this codebase.

---

## Room & setup (inputs, not derived)

### "Ventilation ACH"
- Where shown: docx Room Setup table only (both modes)
- Code field: `settings["ach"]`
- Meaning: the dial-in ventilation rate — sets the inlet velocity
  boundary condition. A target the user typed in, not a measurement.
- Formula: n/a (input)
- Type: **[air]**, nominal
- Issues: **Not qualified as nominal anywhere it's shown.** Sits in the
  same report as several *measured* ACH rows further down — a reader has
  no textual cue that this one is the dial-in value and those are CFD
  results. Suggested fix: rename to **"Ventilation ACH (nominal, dial-in)"**.

### "UV inactivation constant Z" / "Susceptibility constant Z (cm² / mJ)"
- Where shown: docx Room Setup ("UV inactivation constant Z") *and* docx
  results table row 2, both templates ("Susceptibility constant Z
  (cm²/mJ)") — **two different labels for the same input, inside the
  same document**
- Code field: `settings["z-value"]`
- Meaning: UV susceptibility constant for the modeled organism.
- Type: n/a (optics input)
- Issues: pick one name and use it in both places.

### "Target well-mixed steady-state T" (docx) / "Target T_ss (design)" (GUI)
- Where shown: both, but worded differently
- Code field: `target_T_ss`
- Meaning: steady-state mode only — the target equilibrium concentration
  used to size the continuous source's injection rate. A **design
  input** chosen before the run, not a measured result.
- Formula: n/a (input; feeds `compute_source_strength`)
- Type: n/a
- Issues: GUI's `"T_ss"` is raw code jargon; docx's wording is clearer.
  Suggest standardizing on the docx wording everywhere.

### "Set well-mixed steady-state T (entire volume)" ⚠️
- Where shown: docx, decay-mode results table row 3, ONLY
- Code field: none (a hardcoded constant, the value `1`)
- Meaning: **this is actually decay mode's fixed initial condition**
  (the whole room starts at T=1) — it is not a steady-state value at
  all. The label is simply wrong for what it's attached to.
- Type: n/a
- Issues: mislabeled; should read something like **"Initial
  contaminant concentration (uniform, decay mode)"**.

### "Calculated Source injection rate Tinj (T-units/s )" (docx) / "Source injection rate (total, room-wide)" (GUI)
- Where shown: both, worded differently, docx has a stray double space
- Code field: `injection_rate_total` (`G`)
- Meaning: steady-state mode only — total room-wide contaminant
  generation rate. Fixed and known exactly; doesn't depend on
  ventilation or UV.
- Formula: `Su × source_volume`
- Type: n/a
- Issues: cosmetic (double space, symbol `Tinj` vs. plain English) only.

### "Average fluence rate"
- Where shown: both GUI and docx, both modes — **the one field with
  fully consistent wording everywhere.**
- Code field: `fluence_mean`
- Meaning: room-average UV fluence rate — pure optics from the lamp
  calculation, no CFD involved.
- Type: n/a

---

## Air-flow verification (`ach_delivery`)

### "ACH (air) measured" (docx decay row 6) / *(not shown in GUI, either mode)*
- Where shown: decay docx only. Steady-state docx doesn't show this row
  at all. **Neither GUI (decay or steady-state) shows it.**
- Code field: `ach_delivery.measured_ach`
- Meaning: the CFD's *actual* resolved flow rate, measured at the outlet
  patch(es). Verified directly this session against a separate inlet-side
  measurement — they agree to ~0.1% (continuity holds in the converged
  solution).
- Formula: `sum(outlet patches) of phi × 3600 / room_volume`
- Type: **[air]**, measured
- Issues: **the only place a user could ever see the true delivered
  airflow, and it only exists in one of four possible views (decay
  docx).** A user reading the GUI, in either mode, has no way to know
  whether the CFD is even delivering its intended airflow.

### "Ventilation delivery" (docx trust-status table, both modes) / *(not shown in GUI)*
- Where shown: docx only
- Code field: `ach_delivery.measured_ach`, `.nominal_ach`, `.ratio`,
  `.within_tolerance` — rendered as one combined sentence, e.g.
  `"{measured}/hr measured vs {nominal}/hr nominal ({ratio}) - OK/MISMATCH"`
- Meaning: pass/fail check on whether delivered airflow matches nominal
  within tolerance. Caught two real bugs this session (a tangential
  "ceiling" diffuser losing ~47-62% of nominal flow; a mesh
  grid-snapping bug silently shrinking a 0.3m opening to 0.2m).
- Formula: `ratio = measured_ach / nominal_ach`
- Type: **[air]**, ratio of two air quantities
- Issues: **missing from the GUI entirely, in both modes.**

---

## Decay-mode results

Decay mode fits an exponential to a transient decay curve (unweighted OLS
regression of `ln(T)` vs. `t` over the whole recorded curve).

### "eACH_uv, well-mixed (idealized: Z x E_avg)" (GUI) / "Calculated eACH" (docx row 4)
- Where shown: both, very differently worded
- Code field: `eACH_uv_well_mixed`
- Meaning: the **idealized ceiling** — what eACH_uv would be if the room
  were perfectly, instantaneously mixed. Computed straight from the
  lamp/fluence field; never touches CFD.
- Formula: `mean(kUV over all cells) × 3600`
- Type: **[T]**, idealized (no CFD)
- Issues: **docx's `"Calculated eACH"` has no idealized/ceiling
  qualifier at all** — it's the largest number in the table and reads as
  the headline result rather than a theoretical maximum. The GUI's
  wording is correct; the docx isn't. Suggest matching the GUI's wording.

### "eACH_uv, CFD-fit (nominal ventilation ACH)" (GUI) / shown only as a silent fallback under docx row 9
- Where shown: GUI always; docx only when the corrected version (below)
  is unavailable, with no label change to indicate the fallback happened
- Code field: `eACH_uv_effective`
- Meaning: UV's own contribution to the fitted total decay rate, using
  the **nominal** ACH as the ventilation baseline — the older, less
  trustworthy of the two methods.
- Formula: `(λ_total_fitted − ach_nominal/3600) × 3600`
- Type: **[T]**, measured, nominal-anchored

### "eACH_uv, CFD-fit (measured ventilation ACH)" (GUI) / "eACHeff CFD measured " (docx row 9)
- Where shown: both, worded very differently; docx has a trailing space
- Code field: `eACH_uv_effective_corrected`
- Meaning: same as above, but anchored to the **measured** ventilation
  rate — the trustworthy figure.
- Formula: `(λ_total_fitted − ventilation_ach_measured/3600) × 3600`
- Type: **[T]**, measured, measured-anchored
- Issues: docx also appends a 95% confidence interval here
  (`" (95% CI: lo–hi /hr)"`) that **the GUI never shows at all.**

### "Ventilation ACH (measured, UV-off control)" (GUI) / "Effective ACHeff CFD measured" (docx row 7)
- Where shown: both, differently worded
- Code field: `ventilation_ach_measured`
- Meaning: ventilation's own removal rate, measured directly from a
  dedicated UV-off control run (same room, no source, pre-mixed, decay
  under ventilation alone).
- Formula: fitted decay rate of the UV-off control curve
- Type: **[T]**, measured
- Issues: docx's `"ACHeff"` naming collides visually with steady-state
  docx's differently-defined `"EACHeff"` (see steady-state section) —
  same-looking abbreviation, different meaning across modes.

### "Mixing efficiency" (GUI) / *(not shown in docx)*
- Where shown: GUI only
- Code field: `mixing_efficiency`
- Meaning: what fraction of the idealized UV ceiling this real,
  imperfectly-mixed room achieves, nominal-ACH-anchored.
- Formula: `eACH_uv_effective / eACH_uv_well_mixed`
- Type: ratio, **[T]** over idealized
- Issues: **missing from the docx entirely.**

### "Mixing efficiency (using measured ventilation ACH)" (GUI) / *(not shown in docx)*
- Where shown: GUI only
- Code field: `mixing_efficiency_corrected`
- Meaning: same, but using the measured-anchored eACH_uv — the number to
  trust.
- Formula: `eACH_uv_effective_corrected / eACH_uv_well_mixed`
- Type: ratio, **[T]** over idealized
- Issues: **missing from the docx entirely.** The docx instead has a
  similarly-named but differently-defined `"ACH efficiency"` row (next
  entry) — easy to mistake for this one.

### "ACH efficiency" (docx row 8) / *(not shown in GUI)*
- Where shown: docx only
- Code field: none directly — computed inline from `ventilation_ach_measured`
  and `ach_delivery.measured_ach`
- Meaning: **a third, different efficiency ratio** — measured (T-based)
  ventilation rate divided by the CFD-*delivered* air flow rate (not the
  idealized ceiling, and not the nominal dial-in rate).
- Formula: `ventilation_ach_measured / ach_delivery.measured_ach`
- Type: ratio, **[T] over [air]**
- Issues: **sounds like `"Mixing efficiency"` above but is a genuinely
  different calculation with a different denominator.** Also compare to
  steady-state's `"Room ventilation pathogen removal efficacy EACHeff"`,
  which uses yet another denominator (nominal ACH) for a similarly-named
  ratio — three "efficiency"-type ratios across the app, three different
  denominators, easily confused with one another.

### "Total ACH, effective" (GUI) / "Total Room ACHeff+eACHeff" (docx row 10)
- Where shown: both, worded differently, **and computed differently**
- Code field: `total_ach_effective` (GUI); computed inline in docx
- Meaning: total real removal rate (ventilation + UV combined) — but the
  two views use **different ACH bases**.
- Formula: GUI: `ach_nominal + eACH_uv_effective` (nominal basis). Docx:
  `ventilation_ach_measured + eACH_uv_effective_corrected` (measured
  basis).
- Type: **[T]**
- Issues: ⚠️ **the two reports print different numbers for "the same"
  total, from the same run**, because they use different ACH bases under
  near-identical labels. This is the single most concrete
  GUI-vs-docx disagreement found in the audit.

### *(no on-screen label anywhere)*
- Where shown: neither GUI nor docx
- Code field: `total_ach_well_mixed`
- Meaning: total removal rate under the fully idealized assumption
  (nominal ventilation + idealized UV ceiling).
- Formula: `ach_nominal + eACH_uv_well_mixed`
- Type: **[T]**/idealized
- Issues: written to `results.json`, never displayed anywhere.

### "Total average Pathogen reduction in room...per hour " (docx row 12) / "True UVGI Effectiveness" (docx row 15) / "Reduction" (GUI sweep table only)
- Where shown: docx (twice, same number) and the GUI's *sweep progress
  table* only — **not shown at all on the single-run Analysis tab**
- Code field: none in `results.json` — an analytical formula computed
  by the report code itself from the eACH/ACH pair, **not read off an
  actual steady-state CFD run**.
- Meaning: what the steady-state reduction percentage *would* be under a
  well-mixed model, given this run's fitted eACH and ACH. Deliberately
  replaced an earlier "1 − exp(−rate·1hr)" transient formula that could
  show a meaningless ~100% (a real case showed this against a true
  steady-state figure of ~11.9%).
- Formula: `eACH / (ACH + eACH)`
- Type: derived, **[T]**-based
- Issues: row 12's **"per hour" wording is stale** — the per-hour
  version of this formula was explicitly removed; the label was never
  updated to match. Rows 12 and 15 are byte-identical values under two
  different-sounding names.

---

## Steady-state-mode results

Steady-state mode runs to equilibrium under a continuous source: Phase 1
(ventilation only) then Phase 2 (+ UV), reading off each phase's
converged room-average concentration — a mass-balance ratio, not a curve
fit.

### "Phase 1 moving average (no UV, last {span} iterations)" (GUI) / "Steady state T, calculated from moving average ({frac}% of last results)" (docx row 7)
- Where shown: both, worded very differently — **and the window is
  described two different ways** (a raw iteration count vs. a percentage)
- Code field: `phase1.T_ss` (`T_ss1`)
- Meaning: Phase 1's converged room-average concentration (ventilation
  only) — a trailing-window moving average of the live per-iteration
  series.
- Formula: windowed mean, last `window_frac` fraction of samples
- Type: n/a (raw CFD result)

### "Phase 1 CV (no UV, last {span} iterations)" (GUI) / "CV ({frac}% of last results)" (docx row 8)
- Where shown: both; docx drops the "Phase 1"/"no UV" context entirely
- Code field: `phase1.T_ss_cv`
- Meaning: coefficient of variation of the above window — how noisy/
  settled that trailing window is.
- Type: n/a

### "Phase 1 extrapolated T∞ (no UV, n→∞)" (GUI) / "Extrapolated TSS1∞" (docx row 9)
- Where shown: both, different naming convention (`T∞` vs. `TSS1∞`)
- Code field: `phase1.T_inf_extrapolated`
- Meaning: Phase 1's **true** n→∞ value, curve-fit-extrapolated rather
  than just averaged — added specifically because a real case's windowed
  `T_ss1` was caught 8.9% off from its true asymptotic value while still
  "looking" plateaued.
- Formula: single-exponential-approach-to-equilibrium fit to the live
  series
- Type: n/a (raw CFD result)

### "Phase 2 moving average (UV on, last {span} iterations)" (GUI) / "Steady State TSS2, calculated from moving average (last {n} iterations)" (docx row 11)
- Where shown: both; docx switches to a **raw iteration count** here
  after using a **percentage** for the equivalent Phase 1 row — inconsistent
  within the same table
- Code field: `phase2.T_ss` (`T_ss2`)
- Meaning: Phase 2's converged room-average concentration (source + UV).
- Type: n/a (raw CFD result)

### "Phase 2 CV (UV on, last {span} iterations)" (GUI) / "CV (last {n} iterations)" (docx row 12)
- Code field: `phase2.T_ss_cv`
- Meaning: coefficient of variation of the Phase 2 window.
- Type: n/a

### "Phase 2 extrapolated T∞ (UV on, n→∞)" (GUI) / "Extrapolated TSS2∞" (docx row 13)
- Code field: `phase2.T_inf_extrapolated`
- Meaning: Phase 2's true n→∞ value, curve-fit-extrapolated. This is the
  single most load-bearing number in the whole steady-state corrected
  formula (`eACH_uv_steady_state_corrected` depends on it almost
  entirely).
- Type: n/a (raw CFD result)

### "Reduction" (GUI) / "Total average Pathogen reduction in room" (docx row 15) / "True UVGI Effectiveness" (docx row 22) / "Simple UVGI Effectiveness" (docx row 23)
- Where shown: GUI once, docx **three times under three different
  names, all printing the same number** (confirmed intentional in a code
  comment, since "True"/"Simple" only differ by which ACH basis produced
  the eACH/ACH pair that reduces to the same `1 - T_ss2/T_ss1` ratio
  regardless)
- Code field: `reduction_pct` / `reduction_pct_corrected`
- Meaning: percent reduction in steady-state concentration once UV is
  added.
- Formula: `(1 − T_ss2/T_ss1) × 100` (nominal); `(1 − T_ss2/T_ss1_corrected) × 100`
  (corrected, where `T_ss1_corrected = G/(V·ventilation_ach_measured/3600)`)
- Type: ratio
- Issues: bare **"Reduction"** on the GUI never says reduction *of what*
  (steady-state concentration) — easy to misread as a UV dose reduction
  or similar. The docx's three-labels-one-number pattern, while
  intentional, invites a reader to assume three distinct metrics.

### "Effective ventilation ACH (measured, UV-off control run)" **or** "Effective ventilation ACH (well-mixed-equivalent, from Phase 1)" (GUI) / "Effective pathogen (mechanical) ACHeff" (docx row 16)
- Where shown: both; **GUI's label changes depending on which
  measurement method was actually used** (`ventilation_measurement_method`),
  docx's does not
- Code field: `ventilation_ach_measured`
- Meaning: ventilation's own rate. Two possible methods: `"control_run"`
  (preferred — a dedicated UV-off control run, matching decay mode) or
  `"phase1_buildup"` (older, less trustworthy — derived from Phase 1's
  own `T_ss1`, biased by the point-source's mixing-transport lag).
- Formula: control-run: fitted decay rate of a dedicated UV-off run.
  phase1_buildup: `G/(V·T_ss1)`
- Type: **[T]**, measured
- Issues: ⚠️ **docx's word "mechanical" directly contradicts this
  field's own docstring**, which explicitly states this is a
  ventilation-*effectiveness* metric, not a flow-rate/mechanical
  measurement. The GUI's own label gets this right; the docx doesn't.

### "Room ventilation pathogen removal efficacy EACHeff" (docx row 17) / *(not shown in GUI)*
- Where shown: docx only
- Code field: none directly — `ventilation_ach_measured / settings["ach"]`
  computed inline
- Meaning: measured (T-based) ventilation rate as a fraction of the
  **nominal** dial-in ACH.
- Formula: `ventilation_ach_measured / ach_nominal × 100%`
- Type: ratio, **[T] over [air]-nominal**
- Issues: compare to decay mode's `"ACH efficiency"`, which divides by
  the **CFD-delivered** rate instead of nominal — same-sounding concept,
  different denominator, across the two modes.

### "eACH_uv, steady-state CFD-fit (measured ventilation ACH)" (GUI) / "True CFD measured eACHCFD" (docx row 18)
- Where shown: both, worded very differently
- Code field: `eACH_uv_steady_state_corrected`
- Meaning: the trustworthy figure — total mass-balance-implied removal
  rate minus the *measured* ventilation rate.
- Formula: `λ_total_actual − ventilation_ach_measured`, where
  `λ_total_actual = G/(V·T_ss2) × 3600`
- Type: **[T]**, measured-anchored

### "eACH_uv, steady-state CFD-fit (assumes nominal design ACH...)" (GUI) / "Simple CFD measured eACHCFD_s" (docx row 19)
- Where shown: both; **docx never states this is nominal-ACH-based**,
  unlike its GUI counterpart and unlike the adjacent row 18
- Code field: `eACH_uv_steady_state`
- Meaning: UV's contribution to the total removal rate, nominal-ACH-
  anchored (older method, kept for comparison).
- Formula: `(ach_nominal/3600)·(T_ss1/T_ss2 − 1) × 3600`
- Type: **[T]**, nominal-anchored
- Issues: a docx footnote claims `eACHCFD_s = eACHCFD / EACHeff`, which
  is only true on the older `phase1_buildup` measurement path — on the
  current `control_run` path the two use genuinely different `T_ss1`
  bases, so the printed identity doesn't hold for current results.

### "Total ACH in room (ACH+eACH_uv)" (docx row 20) / *(not shown in GUI)*
- Where shown: docx only
- Code field: none directly — `ventilation_ach_measured + eACH_uv_steady_state_corrected`
- Meaning: total measured removal rate (ventilation + UV).
- Type: **[T]**
- Issues: the "ACH" in this label is the **measured/T-based** rate, not
  the nominal one a reader will likely assume from the plain word "ACH".

---

## Ranked list of what to fix first

1. `"Total ACH, effective"` (GUI) vs. `"Total Room ACHeff+eACHeff"`
   (decay docx) — **different formulas, same-sounding label, same run.**
2. `"Effective pathogen (mechanical) ACHeff"` — drop "mechanical", it
   contradicts the field's own docstring.
3. `"Calculated eACH"` (both docx templates) — needs an idealized/
   ceiling qualifier; currently reads as the headline result.
4. `"Simple CFD measured eACHCFD_s"` — must state its ACH basis, like
   its neighbor row does.
5. Three different "efficiency"-shaped ratios (`mixing_efficiency`,
   docx `"ACH efficiency"`, docx `"...removal efficacy EACHeff"`) with
   three different denominators — consolidate to one definition and one
   name, or clearly differentiate all three names.
6. `ach_delivery` (the only air-based verification numbers in the whole
   app) is docx-only, decay-mode-only — bring it into the GUI, both modes.
7. Stale `"...per hour "` wording on the decay reduction-ratio row.
