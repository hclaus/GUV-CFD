# potentialFoam usefulness test

Started: 2026-08-05T14:40:05

**Question**: does potentialFoam's cheap inviscid pre-solve actually save
net time, for SIMPLE and for LTS separately? Hypothesis: LTS's adaptive
per-cell time stepping may make it less dependent on a good starting
guess than SIMPLE, so potentialFoam might help SIMPLE more than LTS (or
even net-negative for LTS).

**Parameters**: room 3.6x4.2x2.7 meters, cell size
0.1m (40,824 cells), iteration cap 4000, rel_tol 1%
(defaults). Fresh case per combination (mesh + initial fields only, no
flow-field reuse across combinations) so potentialFoam on/off is the ONLY
thing that differs at the start.

**Combinations**: `simple_with_pf`, `simple_no_pf`, `lts_with_pf`, `lts_no_pf`

**Case directories**: `\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\potentialfoam_test/<name>/`

---

## Results
