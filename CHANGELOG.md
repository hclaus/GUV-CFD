# Changelog

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
