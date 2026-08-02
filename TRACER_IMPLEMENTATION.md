# Tracer Implementation: Age-of-Air & Segregated-Flow Model

## Overview

This branch (`tracer-implementation`) adds support for residence time distribution (RTD) and dose-distribution analysis based on Blatchley et al. "Inactivation of Aerosol-Laden Viruses Resulting From Exposure to UV-C Radiation."

**Goal**: Predict UV inactivation accounting for the spatial non-uniformity of fluence rate and residence time, rather than assuming perfect mixing.

## What's New

### 1. **Age-of-Air Transport Equation** (CFD Solver)

**File**: `guvcfd/templates/case_template/system/controlDict`

Added a second `scalarTransport` function object (`scalarTransport2`) that solves:

```
∂(age)/∂t + ∇·(U·age) = 1 + ∇·(D∇age)
```

**Physics:**
- `age = 0` at inlets (freshly entering air)
- `age` increases by 1 second per second everywhere (volumetric source = 1)
- At steady state, `age[cell]` = mean residence time of air at that point
- This is the Eulerian age-of-air field (standard ASHRAE approach)

**Boundary Conditions:**
- Inlet: `fixedValue 0` (reset age to 0 at intake)
- Outlet: `zeroGradient` (age leaves with the flow)
- Walls: `zeroGradient` (age doesn't cross solid boundaries)

### 2. **Initial Condition: Age Field**

**File**: `guvcfd/initial_fields.py`

Added "age" to the field specs with:
- `internalField: uniform 0` (starts at 0)
- Time dimension: `[0 0 1 0 0 0 0]` (seconds)
- Reset on scenario restart (added to `_FULL_RESET_FIELDS`)

Auto-generated when `write_initial_fields()` is called during case setup—no manual file creation needed.

### 3. **RTD Extraction Module**

**File**: `guvcfd/age_analysis.py`

Key functions:
- `read_age_field()`: Read age from latest CFD time step
- `compute_age_statistics()`: Mean, std, min, max of age distribution
- `build_rtd_histogram()`: Bin age values into a histogram
- `rtd_from_histogram()`: Convert histogram to RTD density form E(t)
- `ashrae_air_change_effectiveness()`: Compute ε_a = effective_ACH / target_ACH

**Metric**: ASHRAE 129 air-change effectiveness
```
ε_a = (room_volume / mean_age) × 3600 / target_ACH
```
- ε_a ≈ 1.0 → well-mixed (good ventilation uniformity)
- ε_a << 1.0 → stagnant pockets, short-circuiting

### 4. **Dose Distribution & Segregated-Flow Model**

**File**: `guvcfd/dose_distribution.py`

Implements Blatchley's segregated-flow equation:

```
(N/N₀)_reactor = ∫ exp(-k·D) · E(D) dD
```

**Workflow:**
1. Compute fluence rate at each cell: `E[cell]` [mW/cm²]
2. Read residence time (age): `τ[cell]` [s]
3. Compute dose: `D[cell] = E[cell] × τ[cell]` [mJ/cm²]
4. Build dose distribution histogram → E(D)
5. Apply first-order kinetics: `N/N₀ = exp(-k·D)` per dose bin
6. Integrate: overall survival = Σ exp(-k·D_i) · E(D_i) · ΔD_i

**Output:**
- Dose statistics (mean, std, min, max)
- Dose distribution histogram
- Overall inactivation prediction (N/N₀, log reduction)
- Comparison: segregated-flow vs. well-mixed approximation

### 5. **Demo Script**

**File**: `tracer_demo.py`

Stand-alone script demonstrating the workflow:
```bash
python tracer_demo.py /path/to/case --ach 6 --volume 30 --k 0.87
```

Shows:
- RTD extraction and visualization
- ASHRAE effectiveness calculation
- Dose distribution
- Inactivation prediction (segregated-flow model)
- Well-mixed approximation for comparison

## Workflow: Running a Case with Tracer

### 1. Create/Setup Case
Cases are set up as usual via `run_pipeline.setup_case()`. The age field is auto-created:
- Template `controlDict` is copied (includes `scalarTransport2` for age)
- `initial_fields.py` generates `0/age` with correct BCs
- Nothing special needed from the user

### 2. Run the Case
Use pimpleFoam as normal:
```bash
cd case_dir
pimpleFoam > log.pimpleFoam 2>&1
```

Two scalar fields are now solved in parallel:
- `T` (contaminant concentration, existing)
- `age` (residence time, new)

### 3. Post-Process
Extract RTD and dose distribution:
```python
from guvcfd.age_analysis import read_age_field, rtd_from_age_field
from guvcfd.dose_distribution import compute_dose_at_cells, segregated_flow_inactivation
from guvcfd.case_io import read_cell_centers, latest_time_dir
from guvcfd.fluence import compute_fluence_at_points

case_dir = "/path/to/case"
age = read_age_field(case_dir)
rtd = rtd_from_age_field(age)

# Or directly: dose distribution + inactivation
time_dir = latest_time_dir(case_dir)
cell_centers = read_cell_centers(case_dir, time_dir)
fluence = compute_fluence_at_points(case_dir, cell_centers)
dose = compute_dose_at_cells(fluence, age)
# ... build dose distribution, predict inactivation
```

## Connection to Blatchley et al.

The paper establishes that for **laminar flow in a tube** (analytically tractable case):
- RTD can be computed in closed form from Taylor's dispersion solution
- Dose distribution can be derived analytically
- Segregated-flow model ∫ exp(-k·D) E(D) dD predicts overall inactivation

**For your CFD room simulations:**
- Replace analytical RTD with numerical age field
- Replace analytical dose distribution with CFD-computed fluence × age
- Use same segregated-flow integral to predict real (imperfectly-mixed) inactivation
- Compare against well-mixed assumption to quantify the mixing penalty

**Key finding from Blatchley Fig. 11:**
- At low inactivation (~10-20% kill), average dose ≈ dose distribution → well-mixed is OK
- Above ~1 log reduction (90% kill), dose distribution matters → well-mixed is optimistic
- This aligns with your ~200% eACH materiality threshold (when dose/residence time variation becomes significant)

## Files Changed

| File | Change | Purpose |
|------|--------|---------|
| `guvcfd/templates/case_template/system/controlDict` | Added `scalarTransport2` for age | Solver setup |
| `guvcfd/initial_fields.py` | Added "age" field spec | Field initialization |
| `guvcfd/age_analysis.py` | **NEW** | RTD extraction & ASHRAE metrics |
| `guvcfd/dose_distribution.py` | **NEW** | Segregated-flow model & inactivation prediction |
| `tracer_demo.py` | **NEW** | Demo & validation script |

## Testing the Implementation

### Quick Test
```bash
cd GUV-CFD
python tracer_demo.py /path/to/existing/case --ach 6 --volume 30 --k 0.87
```

This reads an existing case and extracts RTD + dose distribution (does not require re-running the case).

### Full Test (New Case with Age)
1. Set up a new case via the app or CLI (with your test case config)
2. Run pimpleFoam
3. Run `tracer_demo.py` on the results

The age field will be auto-written to disk alongside T, p, U, etc.

## Next Steps

### Short term:
- [ ] Validate age field convergence behavior (does it reach steady state?)
- [ ] Compare age-derived RTD against known/measured values for validation
- [ ] Integrate RTD/dose results into the standard `results.json` output
- [ ] Add dose-distribution visualization to the report generation

### Medium term:
- [ ] Add dose-distribution-aware mixing efficiency metric to complement spatial CoV
- [ ] Extend decay_analysis to use segregated-flow prediction instead of well-mixed assumption
- [ ] Build a comparison: "effective eACH_UV from segregated flow" vs. current well-mixed eACH
- [ ] Validate predictions against measured inactivation kinetics (when data available)

### Long term:
- [ ] Integrate age field into Qt app post-processing tabs
- [ ] Support dynamic k values (wavelength-dependent sensitivity)
- [ ] Add Lagrangian particle tracking as alternative RTD method
- [ ] Extend to multicomponent dose (different pathogens with different k)

## References

**Primary:**
- Blatchley III, E. R., Jongewaard, M., Claus, H., Hernandez, M., & Shorno, S. (2023). "Inactivation of Aerosol-Laden Viruses Resulting From Exposure to UV-C Radiation Under Laminar Flow in a Circular Tube." *Environmental Science & Technology*, 57(45), 17393–17403.

**Background:**
- ASHRAE Standard 129-2023: "Measuring Air-Change Effectiveness"
- Taylor, G. I. (1953). "Dispersion of Soluble Matter in Solvent Flowing Slowly through a Tube."

## Contact

Questions or feedback on the tracer implementation? See the branch PR or contact the GUV-CFD team.
