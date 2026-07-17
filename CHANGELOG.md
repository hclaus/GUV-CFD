# Changelog

## 2026-07-17 — Windowed moving-average T_ss for steady-state runs

Real turbulent rooms never fully settle — a single last-sample read of
`volAverage(T)` could be off by 25–50%+ from run to run, especially for
small monitoring-point volumes (confirmed on a real case: "Patient" in
phase 2 had a coefficient of variation of 54%). Room-wide `T_ss` and every
monitoring point now report a trailing-window moving average instead.

- Steady-state runs track room-wide T and every monitoring point **live**,
  every solver iteration, via a controlDict function object spliced in
  alongside the existing UV-decay tracking — not just at the sparse
  `write_interval` snapshots `postProcess` was limited to before.
- `T_ss` (and everything derived from it — `reduction_pct`,
  `eACH_uv_steady_state`, the CFD-measured ventilation/eACH_uv correction)
  is now the mean of the trailing 15% of that live series, with its
  standard deviation and coefficient of variation (CV) reported alongside
  it, instead of a single noisy last-iteration sample.
- New GUI setting, **"T_ss moving-average window (fraction of samples)"**
  (Project Setup → Ventilation & UV), default 0.15 — changeable per
  project instead of hardcoded.
- The `.docx` report and Analysis tab show new **"Moving average (last N
  iterations)"** / **"CV (last N iterations)"** rows for each phase, and
  monitoring-point rows now include their own CV. The phase-timeline chart
  plots the dense live curve with the averaging window shaded.
- Reports generated from case directories that predate this feature fall
  back to the previous plain last-sample display automatically - no
  re-run needed to keep viewing old results.
- Decay-mode runs are unaffected - their key number (`eACH_uv_effective`)
  already comes from a least-squares fit over the full decay curve, a more
  robust method than a plateau read for a curve that's decaying rather
  than settling.

Validated against a real completed run (`patient_ward_4B1_v4`): the dense
live series' final value matched the previous last-sample read exactly
(same physical quantity, confirming the live tracking is correct), while
the windowed average visibly tracked the true plateau far more steadily
than any single sample.
