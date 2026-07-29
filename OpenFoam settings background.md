# OpenFOAM `fvSolution` / `fvSchemes` parameter audit

Background notes from the `nOuterCorrectors`/relaxation investigation on the
UV-off control run (steady-state pipeline, `patient_ward_4B1_v11_simsstate`,
Z=6/ACH=9). Captures what's currently set, what's left at OpenFOAM defaults,
and what each parameter actually influences - for deciding what else might be
worth testing.

## 1. Linear equation solvers (`solvers{}`)

| Field | Set | Influence |
|---|---|---|
| `Phi` | GAMG, tol 1e-6, relTol 0.01, GaussSeidel | Solves the flux-correction potential (mesh-motion/non-orthogonal support solve) |
| `p` | GAMG, tol 1e-6, relTol 0.1, GaussSeidel | Pressure Poisson solve - the core of the pressure-velocity coupling |
| `pFinal` | inherits `p`, relTol 0 | Same, but on the last corrector of a timestep - solved to full `tolerance`, not just 90% |
| `U,k,omega,T` | smoothSolver, tol 1e-8, relTol 0.1, GaussSeidel | The momentum/turbulence/scalar equations, non-final passes |
| `U,k,omega,TFinal` | inherits above, relTol 0 | Same fields, final pass - fully converged |

**Not set (defaults apply):**
- **GAMG internals** - `nCellsInCoarsestLevel`, `agglomerator`, `mergeLevels`,
  `nPreSweeps`/`nPostSweeps`/`nFinestSweeps`. These govern how thoroughly the
  multigrid solver actually resolves the pressure equation per call. Untouched -
  plausible secondary lever, but since `pFinal`/`*Final` already force `relTol 0`
  (full convergence) on the pass that matters, unlikely to be the main effect.
- **`nSweeps`** for `smoothSolver` (GaussSeidel) - how many sweeps per linear
  solve of U/k/omega/T. Not set, so OpenFOAM's built-in default applies. Same
  reasoning: the *Final* variant already drives to full tolerance, so this
  mainly affects speed, not the final answer, **given** enough outer correctors
  are used to reach the Final pass meaningfully (which was exactly the problem
  at `nOuterCorrectors=1`).
- **`minIter`** - floor on solver iterations regardless of residual. Unset;
  rarely matters unless a solve exits suspiciously fast.

## 2. `PIMPLE{}`

| Set | Value |
|---|---|
| `nOuterCorrectors` | 1 (testing 3) |
| `nCorrectors` | 2 |
| `nNonOrthogonalCorrectors` | 0 |
| `residualControl` | added during this investigation - `p`/`U`/`T`, nested dict form (`{tolerance; relTol;}`, NOT the flat scalar form SIMPLE uses) |

**Not set:**
- **`momentumPredictor`** - defaults to `yes` (solve U explicitly before
  pressure correction). Leaving at default is standard/correct here; only
  worth touching for creeping/Stokes-like flows.
- **`turbOnFinalIterOnly`** - defaults to `true` in recent OpenFOAM: k/omega
  are only actually updated on the *last* outer corrector, not every one. With
  `nOuterCorrectors=3`, turbulence quantities are effectively frozen for 2 of
  the 3 passes each timestep. Minor effect here since the flow field is
  already converged going into the control run, but a real "defaults quietly
  matter" case.
- **`pRefCell`/`pRefValue`** - only needed if the domain has no fixed-pressure
  boundary anywhere (fully closed). Correctly irrelevant here (real outlet
  with a pressure BC).

## 3. `SIMPLE{}` (used by Phase 1/Phase 2, not the control run itself)

| Set | Value |
|---|---|
| `nNonOrthogonalCorrectors` | 0 |
| `consistent` | no (i.e. not SIMPLEC) |
| `residualControl` | p/U/(k\|omega) at 1e-4, flat scalar form |

**Not set:** `consistent yes` (SIMPLEC) is a real alternative - removes the
need for pressure under-relaxation entirely by construction, often converges
faster/more robustly than plain SIMPLE with under-relaxed pressure. Worth
knowing about if Phase 1/2 convergence quality becomes the focus again;
out of scope for the control-run issue.

## 4. `relaxationFactors{}`

| Set | Value |
|---|---|
| `fields.p` | 0.3 |
| `equations.U`, `equations.(k\|omega)` | 0.6-0.7 (varies per test) |
| `equations.T` | 1.0 / 0.7 / 0.5 / 0.3 (what's been varied across tests) |

**Not set:** nothing missing that OpenFOAM would silently default differently -
this block only contains what's explicitly relaxed; anything absent simply
isn't relaxed (relaxation factor effectively 1).

## 5. Adjacent file - `fvSchemes` (not `fvSolution`, but directly relevant)

```
div(phi,U)  bounded Gauss linearUpwindV grad(U);   // 2nd-order
div(phi,T)  bounded Gauss upwind;                   // 1st-order
```

**`T` is discretized with plain first-order upwind**, while `U` gets a
second-order scheme. First-order upwind is more numerically diffusive - it
doesn't bias the result in a relaxation-dependent way like the
`nOuterCorrectors` issue does, but it does mean the "true" converged T field
itself carries more artificial numerical diffusion than U's. Independent of
everything tested so far; worth a separate look if the control run's decay
curve needs to be maximally trustworthy, but doesn't explain the
relaxation-sensitivity - that's fully explained by `nOuterCorrectors`.

## Bottom line

Of everything not yet touched, `turbOnFinalIterOnly` and T's first-order
`div` scheme are the two genuinely relevant candidates for follow-up; the
GAMG/smoothSolver internals are lower-priority since the `Final` solver
variants already force full convergence on the pass that counts.

## Experiments run so far (Z=6, ACH=9 UV-off control run)

| `nOuterCorrectors` | `residualControl` | T relaxation | scalarTransport1 `nCorr`/`tolerance` | Measured ventilation ACH |
|---|---|---|---|---|
| 1 | no | 0.7 (original) | default (0 / 1) | 5.28-5.38 /hr |
| 1 | no | 0.5 | default (0 / 1) | 3.716 /hr |
| 1 | no | 0.3 | default (0 / 1) | 2.208 /hr |
| 3 | yes (p/U 1e-4, T 1e-5) | 1.0 | default (0 / 1) | **7.805 /hr** (`oc3_relax1`) |
| 3 | yes (p/U 1e-4, T 1e-5) | 0.7 | default (0 / 1) | **5.367 /hr** (`oc3_relax07`) - unchanged from the original relax=0.7 run despite `nOuterCorrectors` 1->3, confirming PIMPLE-side changes don't touch T at all |
| 3 | yes (p/U 1e-4) + `turbOnFinalIterOnly false` + T div 2nd-order | 1.0 | default (0 / 1) | **8.325 /hr** (`oc3_full`) |
| 3 | yes (p/U 1e-4) | 0.7 | **3 / 1e-4** | **7.866 /hr** (`stnc_relax07`) |

## CONFIRMED: the scalarTransport1 bug fully explains the relaxation sensitivity

`stnc_relax07` (relax=0.7, but `scalarTransport1` given `nCorr=3`/`tolerance=1e-4`
so it actually iterates - confirmed in the log: `outer iteration: 3`,
`initial-residual tolerance: 0.0001`, vs the trivial `outer iteration: 1`/
`tolerance: 1` every other run showed) landed at **7.866 /hr**, within 0.8% of
`oc3_relax1`'s **7.805 /hr** (relax=1.0, no relaxation at all). Two completely
different routes to "make T's relaxation factor stop mattering" converging to
the same answer is definitive: **T's relaxation factor was never a real
physical tuning knob in this control run** - it was purely an artifact of
`scalarTransport1` never actually iterating.

**The room's true ventilation delivery at ACH=9 nominal is ~7.8-8.3 /hr
against 8.90 delivered - i.e. ~87-93% efficiency**, not the ~59-63%
"mechanical mixing efficiency" this investigation had been computing from
every biased control run (original V10/V11 sweeps included - both used the
same template with these same defaults). Every "measured ventilation ACH,"
"corrected eACH_uv," "corrected reduction%," and "mechanical mixing
efficiency" number computed anywhere in this investigation from a control
run is understated by this bug, not reflecting a genuine mixing problem.

One secondary, real effect: `oc3_full` (8.325) sits ~6.7% above `oc3_relax1`
(7.805) despite both being bias-free - attributable to `turbOnFinalIterOnly
false` and/or T's 2nd-order `div` scheme (not separated by an isolating run,
but both are worth keeping in the final config regardless).

**Next step (awaiting go-ahead):** apply the fix to the actual pipeline -
`guvcfd/templates/case_template/system/controlDict`'s `scalarTransport1`
block needs `nCorr`/`tolerance` set as real values (not left at OpenFOAM's
defaults of 0/1), ideally exposed as an advanced setting
(`app_settings.py`/`app.py`, matching the `momentum-relaxation`/
`scalar-relaxation` pattern) rather than just hardcoding `equations.T`
relaxation to 1.0 - this affects every UV-off control run in both decay mode
and steady-state mode, since they share this template.

No saturation was observed across the `nOuterCorrectors=1` sweep - the
measured ACH kept dropping roughly proportionally to the relaxation factor.

## IMPORTANT CORRECTION: `T` isn't solved inside the PIMPLE loop at all

Confirmed directly from OpenFOAM's installed source
(`src/functionObjects/solvers/scalarTransport/scalarTransport.C`, OpenFOAM
v2412): `T` is computed by the **`scalarTransport1` function object**
(`system/controlDict`'s `functions{}` block), which executes once per
timestep (`executeControl timeStep; executeInterval 1;`) - entirely
*outside and after* PIMPLE's own U/p/k/omega outer-corrector loop. Confirmed
in the actual solver log: `scalarTransport execute: T` prints only after
`PIMPLE: not converged within 3 iterations`.

This means **`PIMPLE.nOuterCorrectors` and `PIMPLE.residualControl` have NO
effect on how many times T itself gets solved** - the `residualControl.T`
entry added to the `PIMPLE` block earlier in this investigation is inert for
this field. The real mechanism, straight from the source:

```cpp
scalar relaxCoeff = 0;
mesh_.relaxEquation(schemesField_, relaxCoeff);  // reads relaxationFactors.equations.T
...
for (int i = 0; i <= nCorr_; ++i) {
    ...
    sEqn.relax(relaxCoeff);
    converged = (sEqn.solve(schemesField_).initialResidual() < tol_);
    if (converged) break;
}
```

`nCorr_` defaults to **0** (loop runs exactly once) and `tol_` (the
function object's own `tolerance` entry) defaults to **1** (so
`initialResidual() < 1` is essentially always true - it "converges" on the
first pass no matter what). `equations.T`'s relaxation factor is genuinely
applied every pass (confirmed: `relaxCoeff` comes from
`mesh_.relaxEquation("T", ...)` = the same `fvSolution` entry we've been
varying) - but with only one pass and a no-op convergence check, there's no
iteration for that relaxation to "converge through."

**The two correct fixes, precisely located:**
1. **`equations.T` relaxation = 1.0** - `sEqn.relax(1.0)` is a no-op, so the
   single default pass is already the fully-implicit, unbiased answer. No
   `scalarTransport1` changes needed. (This is what `oc3_relax1` already
   tests, and turns out to be the fully correct fix on its own, not a
   simplification.)
2. **Keep T relaxed, but make `scalarTransport1` actually iterate** - set
   `nCorr` (e.g. 3) and `tolerance` (e.g. 1e-4) *inside the `scalarTransport1`
   block in `system/controlDict`*, not `PIMPLE`. This is the direct
   equivalent of "more outer correctors + a real residual target," just
   located in the right place. `stnc_relax07` (relax=0.7, `nCorr=3`,
   `tolerance=1e-4`) tests this - if the mechanism above is the whole story,
   its measured ACH should land close to `oc3_relax1`'s, not the ~3.7 /hr the
   original uncorrected relax=0.7 run gave.
