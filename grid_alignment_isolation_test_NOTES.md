# Grid-alignment isolation test — 2026-08-19

## Purpose

Test whether the confirmed inlet/outlet grid-misalignment bug (see
`guvcfd/mesh_gen.py`'s `_actual_axis_cell_size`/`_opening_box`, drafted
2026-08-19, not yet committed) is a cause of the strong Phase 2
oscillation seen at 0.1m cell size and absent at 0.09m/0.08m — or
whether it's an unrelated correlate. Method (user's design): rebuild the
*same* `4x5x32_B15cell009.guvcfd` project (0.09m nominal cell size,
ACH=6, Z=3) with the draft fix applied, so the inlet/outlet land on the
real per-axis mesh grid instead of the wrong nominal-cell_size grid, and
see if oscillation reappears. Fix applied temporarily for this one run
only, then reverted — see [[project structure]] below.

## Room mesh (unaffected by the fix — same at 0.1m/0.09m/0.08m, only
reported here for reference)

4m x 5m x 3m room, nominal cell_size=0.09m:
- cell counts: nx=44, ny=56, nz=33
- **actual** per-axis cell size: dx=0.090909m, dy=0.089286m, dz=0.090909m
  (not 0.09m exactly on any axis — `round(L/cell_size)` doesn't divide
  each dimension evenly)

## Inlet (xMin wall) — nominal center y=2.5/z=2.55, nominal size 0.4x0.4

| | y range | width | z range | height | area | cells |
|---|---|---|---|---|---|---|
| nominal (as typed) | [2.300, 2.700] | 0.400 | [2.350, 2.750] | 0.400 | 0.160 m² | n/a |
| **today's actual build** (bug: snaps to nominal 0.09 grid) | [2.250, 2.700] | 0.450 | [2.340, 2.790] | 0.450 | ~0.203 m² (real) | 25 (5x5) |
| **this test's build** (fix: snaps to real per-axis grid) | [2.232, 2.768] | 0.536 | [2.273, 2.818] | 0.545 | 0.292 m² | 36 (6x6) |

## Outlet (xMax wall) — nominal center y=2.5/z=0.45, nominal size 0.5x0.4

| | y range | width | z range | height | area | cells |
|---|---|---|---|---|---|---|
| nominal (as typed) | [2.250, 2.750] | 0.500 | [0.250, 0.650] | 0.400 | 0.200 m² | n/a |
| **today's actual build** (bug) | [2.250, 2.790] | 0.540 | [0.180, 0.720] | 0.540 | ~0.292 m² (real) | 36 (6x6) |
| **this test's build** (fix) | [2.232, 2.768] | 0.536 | [0.182, 0.727] | 0.545 | 0.292 m² | 36 (6x6) |

## Important caveat — this is NOT a pure alignment-only isolation

The fix changes the inlet's real cell count from 25 to 36 (+44% area) —
not just its position. The outlet barely changes (36→36 cells, position
shifts only ~2-7mm) because its particular nominal size/position
combination happened to be close to a real-grid coincidence already. So
this test conflates "inlet repositioned" with "inlet also meaningfully
enlarged" — if oscillation disappears, we can't cleanly attribute it to
alignment alone vs. the inlet's incidentally-larger, better-resolved
opening. Flagged to the user before running; a fully clean isolation
(alignment-only, size held exactly constant) would need Option B
(bypass script with hand-picked real-grid-exact nominal-size values) as
a follow-up if this result is ambiguous.

## Source zone (source-zone-size=0.8, center 2.0/2.5/1.5)

NOT changed for this test — `_source_box`'s own version of this bug
(contaminant_source.py) is still on the "later" list, unfixed. Its
distortion is much smaller in practical effect (~1% on total injected
G, see prior analysis) than the opening bug, so left as-is here rather
than compounding two changes in one test.

## Run details

- Project file: `C:\Users\hukcl\Documents\OpenFoam\4x5x3\4x5x32_B15cell009.guvcfd`
  (UNCHANGED — same nominal design values; only the code processing them
  is temporarily different)
- New case dir (does not overwrite the existing, already-analyzed
  0.09cell run): `.../run/4x5x32_B15DUSS_6ACH09cell_gridfix`
- Same ACH=6, Z=3, maxCo=5, deltat-scaling-enabled=True, monitor points,
  everything else identical to the existing 0.09cell run.

## Result — 2026-08-19: NEGATIVE for the alignment hypothesis

Real, direct mesh inspection confirmed the fix built exactly as predicted:
`inlet` and `outlet` both carved as clean 6x6=36-real-cell patches (up
from the misaligned run's 25-face inlet / 36-face outlet), landing
exactly on the real per-axis grid lines.

Phase 2 (the stage that actually oscillates/fails to converge at 0.1m):

| | grid-fixed 0.09m | original misaligned 0.09m | 0.08m | 0.10m |
|---|---|---|---|---|
| `converged` | **True** | True | True | **False** |
| `T_ss_cv` | 0.466% | 0.110% | 0.527% | 1.962% |
| `fit_cv` | 0.52% | 0.096% | 0.63% | n/a (rejected) |
| plateau peak-to-peak/mean | 1.56% | 1.42% | 1.89% | 7.25% |
| `reduction_pct` / `eACH_uv` | 61.61% / 9.63 | 60.46% / 9.17 | 61.62% / 9.63 | 50.14% / 6.03 |

**Conclusion**: correcting the grid-alignment bug while holding 0.09m
resolution fixed did NOT reintroduce oscillation — Phase 2 stayed
cleanly converged and flat, in the same regime as both correctly-
resolved cases (0.08m and the original misaligned 0.09m), and nothing
like 0.1m's non-convergent multi-cycle swings. If anything, the
grid-fixed run's plateau is slightly *noisier* (0.466% vs 0.110%) than
the misaligned original, not flatter — a small point against, not for,
the alignment hypothesis. Per the user's own stated criterion, this is
the "not proven" outcome: grid misalignment is ruled out as the cause of
the 0.1m oscillation. The mesh-resolution hypothesis (cells-across-jet,
shear-layer resolution) remains the leading, still-not-definitively-
isolated candidate.

Draft fix reverted after this test per the "later, batch all fixes"
plan (see repo TODO list) — not left applied in either repo's working
tree.
