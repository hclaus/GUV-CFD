# Changelog

## 2026-07-18 — Surface Extrapolated T∞ in report/Analysis tab; derive ACH from it

- `.docx` report and Analysis tab now show a **"Phase N extrapolated T∞
  (n→∞)"** row (with its own fit CV) alongside the existing moving-average
  row, whenever `fit_asymptotic_value` succeeded - both `app.py`'s
  Analysis tab and `report.py`'s `.docx` export already shared the same
  `_phase_ss_rows` helper, so one change covers both.
- Since `ventilation_ach_measured`/`eACH_uv_steady_state(_corrected)` are
  derived from Phase 1/2's T_ss ratio, they inherit whatever bias T_ss
  has - now computed from the extrapolated T∞ instead of the windowed
  average whenever *both* phases produced one (falls back to the
  windowed average otherwise, unchanged from before). A new
  `ach_source` field records which was used; the report/Analysis tab
  note it directly on the Reduction and measured-ACH/eACH_uv rows.

## 2026-07-18 — Detrended CV reporting; exponential extrapolation for T_ss

While reviewing the validated run's numbers, checked whether the reported
windowed CV was measuring genuine noise/fluctuation or was partly
contaminated by a still-slowly-changing mean. It was: fitting a linear
trend to phase 1's trailing window found the drift over the window
(2.2% of the mean) was 3.4x the raw std - most of the reported "0.64%
CV" was systematic drift, not noise. Comparing window widths (5%/10%/15%)
showed the windowed *mean* itself kept shifting with window width too
(1.960 -> 1.954 -> 1.946), confirming none of them had reached a truly
flat plateau within this run's (deliberately reduced, for validation
speed) iteration budget.

- New `decay_analysis.windowed_stats_detrended()`: same trailing-window
  mean as `windowed_stats()`, but std/CV are computed from the residual
  after removing a linear trend fit to the window - isolates genuine
  fluctuation from a still-drifting average. Now what `T_ss_std`/`T_ss_cv`
  actually report (room and monitoring-point summaries both). Plateau/
  convergence detection is deliberately unaffected - `check_plateau_windowed`
  still uses the raw (non-detrended) statistic.
- New `decay_analysis.fit_asymptotic_value()`: fits `T(n) = T∞ - A·e^(-n/τ)`
  (single-exponential approach to equilibrium - the natural shape of
  SIMPLE's outer-iteration convergence) over the trailing half of the
  live series, and extrapolates to the true n→∞ value. A windowed average
  is provably biased whenever the curve hasn't fully flattened within the
  given iteration budget; on the validated run's phase 1, every windowed
  average tried (5%-15%) was ~3% below this fit's `T∞` (2.026), despite
  an excellent fit (0.04% residual). Reported as **"Extrapolated T∞"**
  alongside the existing windowed `T_ss` (both shown - not a replacement),
  `None` when the fit doesn't converge or the data isn't well-described by
  a single exponential (treated as "unavailable," not an error).

## 2026-07-18 — Validate ceiling-diffuser fix; T under-relaxation; consolidate plateau check

Re-ran the originally-failing project (`patient_ward_4B1_v5`) end-to-end
with the geometry fix from the entry below and `ceiling` diffuser type
restored. Phase 1 completed cleanly (`T_ss=1.95`, sane), but Phase 2
(source + UV) still diverged early (`Time=362` of 2500) - confirmed via
an isolation run that `direct` diffuser type handles this same UV-on
phase fine, so the remaining instability was still connected to the
ceiling diffuser's flow field, just triggered specifically once the UV
sink `fvOptions` terms were added on top of it.

Root cause: `fvSolution` had **no under-relaxation factor for `T`** at
all (only `p`/`U`/`k`/`omega` were relaxed) - a stiff/strong source or
sink term interacting with an imperfectly-smooth flow field is a classic
case for outer-iteration instability, and under-relaxation is the
standard fix. Added `T 0.7;` to `relaxationFactors.equations` (both the
real template and `splice.py`'s fallback `_SIMPLE_BLOCK`). Re-validated
against the same project: **both phases now complete cleanly**, Phase 2
even reports "plateaued" (previously impossible to reach).

- New `splice.set_relaxation_factors(case_dir, momentum_factor,
  scalar_factor)` and two new advanced settings (Settings menu, right of
  File): **Momentum/turbulence relaxation** (U/k/omega, default 0.7) and
  **Contaminant (T) relaxation** (default 0.7) - both GUI-exposed and
  documented with a note on what under-relaxation does and why lowering
  them is the first lever to reach for if a run oscillates/diverges
  instead of settling.

While investigating, found the "plateaued"/"NOT YET PLATEAUED" log
message was checking a **different, staler statistic** than the actual
reported `T_ss`: the old `check_plateau()` used the last 5 *sparse*
`postProcess`-cadence samples' `(max-min)/mean` spread, while `T_ss`
itself comes from `windowed_stats()`'s dense, every-iteration trailing-
15%-of-samples mean/CV (the "windowed moving-average T_ss" feature from
2026-07-17). These two could - and on a real run, did - disagree: 2.32%
sparse spread ("NOT YET PLATEAUED") vs. 0.69% dense CV (clearly settled).
Replaced `check_plateau()` with `check_plateau_windowed()`, using the
exact same statistic as the reported value, so the log message and the
result can never contradict each other again. This also removed the now-
redundant `plateau-window` setting (a separate "how many sparse samples"
knob) - `window_frac` (already used for `T_ss`'s own window, and now for
the convergence check too) replaces it.

## 2026-07-18 — Fix ceiling-diffuser instability; revert to opt-in

A real steady-state run (`patient_ward_4B1_v5`, 0.6x0.3m ceiling-diffuser
inlets on a 3.2x4.8x2.57m room) diverged catastrophically: `T` grew
without bound (`phase2 T_ss` reached `6e+263`) partway through Phase 1,
producing garbage results (`eACH_uv_steady_state = -6.0`) without ever
raising an error. An isolation re-run of the identical project with
`inlet_diffuser_type="direct"` completed cleanly (`T_ss` 0.95/0.14,
`eACH_uv` 33.6/hr - all physically sane), confirming the divergence was
specific to the ceiling-diffuser BC, not a pre-existing issue with this
room's geometry.

Root cause: `compute_radial_inlet_velocities()`'s per-face direction was
simply each face's own literal `(face_center - opening_center)` offset,
normalized - a real problem, not just an edge case, for two reasons:

- **Singular at the exact center.** Direction is undefined at r=0 and
  rotates through the full circle in an arbitrarily small neighborhood
  around it. Any mesh with an even face count along an axis puts two
  faces immediately straddling that center - completely ordinary, not a
  contrived case - and they got assigned near-opposite directions (0.537
  m/s apart out of a possible 0.556 m/s, on the failing project's actual
  opening) despite being physically adjacent cells. That velocity
  discontinuity destabilized the downstream scalar transport solve.
- **Grid-layout-dependent coverage.** Which angles get covered depended
  entirely on the mesh's discrete face positions - an even-width grid
  never puts a face exactly on a cardinal axis, so no face ever pointed
  straight out, which both looks wrong and doesn't match a diffuser
  meant to push air uniformly through the *whole* compass.

Fix, in `initial_fields.compute_radial_inlet_velocities()` (went through
two iterations - see below for the final design):

- **Shape-normalized angle.** Each face's offset from the opening center
  is divided per-axis by the opening's own true half-width/half-height
  (`mesh_gen.opening_half_extents()`, a new helper deriving it from the
  same snapped box `opening_center()` uses) before its polar angle is
  measured - stretching a rectangular opening into a unit circle. This is
  a purely local, continuous per-face formula (no global sort/rank step
  needed): any face sitting exactly on the opening's real midline comes
  out exactly cardinal, not just one arbitrarily-chosen mesh face.
  (An intermediate version instead sorted all faces by raw angle and
  redistributed evenly around the circle - it fixed cardinal-direction
  coverage but couldn't reduce the worst adjacent-face jump, since two
  faces genuinely straddling the center stay maximally far apart in
  sorted order no matter how angles are redistributed. Superseded by this
  shape-normalized approach.)
- **Radius-based tilt taper**, in the same shape-normalized coordinates.
  New `center_angle_deg=90` parameter: tilt blends from straight-into-
  the-room (90°, no radial component) at the opening's exact center up
  to `surface_angle_deg=15` (strong radial spread) at its true physical
  edge - a real diffuser has a solid hub at its center, not an open-air
  discontinuity, so nothing should push hard in either direction right
  there. Using the *true* half-extents (not the mesh's own sampled face
  extremes, which under-reach the real edge by half a cell) matters here:
  using sampled extremes instead measured a worse 0.329 m/s worst-case
  jump on the failing project's actual opening; true extents + Euclidean
  radius measured 0.233 m/s, down from the original 0.537 m/s (a real,
  if partial, reduction) - every face still gets exactly `v_mag`
  magnitude regardless of the tilt blend.
- Investigated raising `surface_angle_deg` above 15° as an additional,
  independent lever (steeper tilt = smaller in-plane component
  everywhere = smaller worst-case jump, trading off against the
  originally-intended strong radial/surface-hugging spread): 20°→0.219,
  25°→0.204, real but modest further reduction. Left at 15° for now
  pending a decision on that tradeoff.

**Default reverted to `direct`** (both the GUI dropdown and
`_NEW_FIELD_DEFAULTS`) until this fix is re-validated against a real,
long steady-state run of the originally-failing project - the taper
reduces but hasn't yet been proven to eliminate the instability.
`ceiling` remains available as an opt-in.

## 2026-07-17 — Surface-attached ceiling/wall diffuser inlet, mesh-grid alignment fix

The inlet boundary condition used to be a single uniform vector straight
into the room ("direct jet") — not how a real diffuser behaves. Added a
2nd preset, **surface-attached (ceiling/wall diffuser)**: a per-face
radial velocity field (computed from the inlet patch's real face
geometry, read directly from `constant/polyMesh`), spreading outward
along the plane of the mounting wall and tilted 15° into the room
(Coandă-effect discharge). Validated for round/square ceiling, vortex,
and grille diffusers per Srebric & Chen 2002 (*HVAC&R Research* 8(3),
"Simplified Numerical Models for Complex Air Supply Diffusers") — the
dominant real-world HVAC diffuser types. Confirmed working end-to-end
against a real OpenFOAM run (mesh generation, `simpleFoam` flow
convergence, `pimpleFoam` transient UV-decay all solve cleanly with the
new `nonuniform List<vector>` boundary condition) before making it the
new default (`direct` remains available as the opt-out).

While visually inspecting a diffuser case in ParaView, found and fixed a
**pre-existing mesh-grid alignment bug**, not new to this feature but
newly *visible* because per-face geometry is now rendered directly: inlet/
outlet openings, the contaminant source zone, and monitoring-point zones
are all carved via `topoSet`'s `boxToFace`/`boxToCell`, which needs exact
box-edge coordinates. Whenever a feature's center coincides with a mesh
vertex (e.g. a room's exact center, if both dimensions divide evenly by
the cell size) *and* its size needs an odd cell count (which can't
straddle a vertex symmetrically), the raw box edges land almost exactly on
a grid line — a floating-point boundary tie for `topoSet`, where edge
cells get included/excluded almost arbitrarily. This produced a lopsided,
irregular carved patch/zone (observed as an inlet patch shaped like a
"ring with a hole," missing its center cell) instead of a clean block.

- `mesh_gen._opening_box`, `contaminant_source.source_topo_set_dict`, and
  `monitoring_points.monitoring_topo_set_dict` now snap every box edge
  independently to the nearest mesh grid line before writing the
  `topoSetDict` — equivalent to shifting the center by up to half a cell
  on whichever side(s) need it, rather than requiring users to pick
  sizes/positions that divide evenly. A no-op for already-aligned
  geometry.
- `opening_center()` (used by the ceiling-diffuser radial direction math)
  derives from the same snapped box, so the computed radial center always
  matches the real carved patch.
- New GUI note next to inlet/outlet/source position fields: entered
  values may shift by up to half a cell to align with the mesh grid.

See README's new "Mesh-grid alignment" section for the full explanation.

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
