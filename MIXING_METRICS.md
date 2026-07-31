# Mixing metrics: definitions, formulas, and the fan-speed investigation

This documents every "how well mixed is this room" number GUV-CFD computes,
what question each one actually answers, and the fan-speed comparison that
motivated writing this down (2026-07-31).

## The four distinct questions

It's easy to conflate these - they can (and did, in the case below) point in
opposite directions on the same run.

| Metric | Question it answers | Basis |
|---|---|---|
| `mixing_efficiency` / "Measured UV eff. %" | Does the real, imperfectly-mixed room deliver as much UV killing as an idealized, perfectly-mixed room would? | UV-specific |
| `mechanical_mixing_efficiency_pct` / "Mechanical mixing eff. %" | Does the air that's actually flowing through the room (verified flow rate) show up as effective contaminant removal, or does it short-circuit? | Ventilation-only, UV-independent |
| `spatial_cov` / "Spatial CoV" | Is the concentration the same everywhere in the room right now, or are there stagnant/hot pockets? | Spatial snapshot, UV-independent |
| `T_ss_cv` (windowed CV, pre-existing) | Has the room-average concentration settled down over the run's iterations? | Temporal, a *convergence* check - not a mixing-quality metric at all |

The last row is included because it's easy to mistake for a mixing metric -
it isn't. It's purely "has this run's own average stopped changing," computed
from the trailing window of the room-average time series. It says nothing
about whether the room is spatially uniform.

## 1. Measured UV eff. % (`mixing_efficiency`)

```
mixing_efficiency = eACH_uv_effective / eACH_uv_well_mixed
```

- `eACH_uv_well_mixed`: an idealized upper bound, computed purely from the
  fluence-rate field and the room's UV susceptibility (Z) - "if the room
  mixed instantly and perfectly, this is the UV removal rate you'd get."
  No CFD/flow involved.
- `eACH_uv_effective`: the *actual*, CFD-measured UV removal rate - a
  regression fit to the real concentration-decay curve (decay mode) or the
  T_ss1/T_ss2 ratio (steady-state mode), with the measured ventilation rate
  subtracted out.

100% = the room delivers UV's full theoretical potential. Below 100% = real
turbulent/imperfect mixing reduced UV's effective impact below the idealized
well-mixed prediction.

Stored as `mixing_efficiency`/`mixing_efficiency_corrected` (decay mode,
`decay_analysis.write_results_summary`) or computed on demand as
`uv_efficiency_pct` (steady-state, `report.py::combo_summary_metrics` -
never persisted directly in steady-state's own results.json).

## 2. Mechanical mixing eff. % (`mechanical_mixing_efficiency_pct`)

```
mechanical_mixing_efficiency_pct = ventilation_ach_measured / ach_delivery.measured_ach * 100
```

- `ach_delivery.measured_ach`: the actual **volumetric flow rate** delivered
  through the outlet, verified directly from the solved flux field
  (`run_pipeline.check_ach_delivery`) - a pure flow-conservation check,
  nothing to do with mixing.
- `ventilation_ach_measured`: the room's actual **effective concentration-
  removal rate**, from either the decay curve fit or a dedicated UV-off
  control run.

If air short-circuits (flows straight from inlet to outlet without properly
sweeping the rest of the room), `measured_ach` can read as fully delivered
while the effective removal rate reads lower - that gap *is* imperfect
mechanical mixing, entirely independent of UV.

Conceptually the same comparison ASHRAE Standard 129 (air-change
effectiveness / age-of-air) makes - measured age-of-air vs. the nominal
value for a perfectly-mixed room - just derived here from a CFD decay-curve
fit rather than a physical tracer-gas test.

Implemented in `decay_analysis.mechanical_mixing_efficiency_pct(result)`,
stored in `results.json` for both sim types, single-run and sweep.

## 3. Spatial CoV (`spatial_cov` / `spatial_cov_final`)

```
spatial_cov = std(field values across every mesh cell) / mean(same)
```

A genuinely different kind of statistic from everything else here: computed
across **space** (every cell, one instant in time), not across iterations or
between two scalar rates. 0 = perfectly uniform concentration everywhere;
higher = more spatially segregated. A commonly-cited rule of thumb in CFD
mixing-uniformity literature puts CoV above ~0.6 as poor mixing.

- **Decay mode**: `spatial_cov_final` - CoV of the final T snapshot (the
  highest-numbered time directory) at the end of the UV-on run.
- **Steady-state mode**: computed **per phase** - `phase1.spatial_cov` (from
  `phase1_T.snapshot`, saved specifically for this) and `phase2.spatial_cov`
  (from the final kept time directory) - since Phase 1 (no UV) and Phase 2
  (UV on) can have genuinely different spatial patterns.

Implemented in `decay_analysis.spatial_coefficient_of_variation(cell_values)`
plus `case_io.read_latest_time_field(case_dir, field_name)` (finds the
highest-numbered time directory and reads its field).

**Caveat for decay mode specifically**: unlike steady-state's Phase 1/2
(a true converged, static field - the mean isn't moving), decay mode's CoV
is read from a field whose *mean* is actively decaying toward zero. As the
mean shrinks, relative noise can inflate the CoV, and the "final" value
depends somewhat on exactly how long the run went (a longer run reads a
higher CoV even under otherwise-identical mixing, simply because the
denominator is smaller). Treat decay-mode's `spatial_cov_final` as
comparable *within* a set of runs sharing the same target end-time/fraction
settings, not as an absolute, run-length-independent number. If tighter
comparability is needed later, computing CoV at a fixed physical time (or
fixed fraction of `combined_end_time`) instead of "whatever the last
snapshot happens to be" would remove this confound.

## Fan-speed investigation results (2026-07-31)

Same room, same ACH=6/Z=6, only fan speed varied (`patient_ward_4B1_v11_
simdecay1[_w_f/_w_f2]`):

| | No fan | Fan 0.3 m/s | Fan 0.5 m/s |
|---|---|---|---|
| Measured UV eff. % (mixing_efficiency) | 64.0% | 59.2% | 60.7% |
| Mechanical mixing eff. %* | - | - | - |
| Spatial CoV @ t=5s | 0.092 | 0.086 | 0.087 |
| Spatial CoV @ t=50s | 0.410 | 0.193 | 0.156 |
| Spatial CoV @ t=95s (final) | **0.551** | **0.217** | **0.162** |

*(mechanical_mixing_efficiency_pct wasn't computed for these three runs -
`ach_delivery` was missing from their results.json due to the dropped-field
bug fixed alongside this feature; re-run to get this column populated.)*

**Reading:** the fan clearly *does* improve spatial uniformity - final CoV
drops by more than half at 0.3 m/s and further at 0.5 m/s, monotonically
with fan speed. But the UV-specific metric moved the *opposite* direction,
dropping from 64.0% (no fan) to 59-61% (fan on).

**These aren't contradictory** - they're answering different questions. A
plausible reconciliation: the fan may homogenize concentration by *rapidly
cycling* air past the UV zone rather than letting it linger there long
enough to accumulate UV dose - evening out the room's overall concentration
profile while reducing residence time in the high-fluence region
specifically. A room can become more spatially uniform while becoming less
favorable for UV exposure, if the mixing mechanism speeds transit through
the lamp zone rather than dwelling in it.

**Practical takeaway:** don't read "mixing efficiency" (UV) as a general
mixing-quality indicator, and don't assume a fan (or any mixing intervention)
that improves spatial uniformity will also improve UV performance - check
both numbers.

## References

- [ANSI/ASHRAE 129-1997 (RA 2002) - Measuring Air-Change Effectiveness](https://webstore.ansi.org/standards/ashrae/ansiashrae1291997ra2002)
- [A new method for air exchange efficiency assessment including natural and mixed mode ventilation (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC8511897/)
- [Air-change effectiveness of overhead air distribution and DV in healthcare](https://www.priceindustries.com/content/uploads/assets/literature/technical-papers/air-change-effectiveness-of-overhead-air-distribution-and-dv-in-healthcare.pdf)
- [Mixing performance test and uniformity analysis (PMC) - CoV as a mixing-uniformity index](https://pmc.ncbi.nlm.nih.gov/articles/PMC12184945/)
- [Indoor pollutant mixing time in an isothermal closed room: a CFD investigation](https://www.sciencedirect.com/science/article/abs/pii/S135223100300774X)
