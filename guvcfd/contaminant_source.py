"""Continuous contaminant source for steady-state scenarios: a small,
always-on cellZone with a positive scalarSemiImplicitSource, representing
e.g. a continuously-shedding occupant (Wells-Riley-style continuous point
source). No mesh/patch changes needed - a topoSet-carved cellZone, distinct
from (and coexisting with) the UV sink cellZones in cellzones.py.

Two phases share this source, staying on throughout:
  Phase 1 (no UV): T starts at phase1_t_initial (0 by default - see
    steady_state_pipeline.run_steady_state_scenario), builds up/settles to
    a steady state set by the balance between this source and ventilation
    removal alone.
  Phase 2 (UV on): starting from phase 1's converged T, UV cellZones are
    added on top of the still-active source, reaching a new, lower steady
    state.
"""
import re

from .decay_analysis import fit_asymptotic_value
from .mesh_gen import snap_outward, _actual_axis_cell_size, _mod_phase, _nearest_lattice_point
from .wsl_utils import wsl_path, run_wsl_or_raise, run_wsl, write_case_file as _write_case_file


def _source_box(center, size, cell_size=None, room_dims=None):
    """((xlo,ylo,zlo), (xhi,yhi,zhi)) for the source zone's box, snapped
    OUTWARD to the mesh grid if cell_size is given (floor each low edge,
    ceil each high edge) - see mesh_gen._opening_box's docstring for why
    outward, not to-nearest: a center/size combination that doesn't land
    on a whole number of cells puts the raw box edges right on a
    boxToCell floating-point boundary tie, and round-to-nearest can
    resolve that tie inward on every axis, silently shrinking the carved
    zone well below the requested size (confirmed on a real case: a 0.3m
    source zone came out as 0.3x0.2x0.2m = 0.012 m^3, 44% of the requested
    0.027 m^3, because two of its three axes hit exactly this tie).
    Snapping outward instead guarantees the carved zone always contains
    the requested box.

    room_dims: (Lx, Ly, Lz), REQUIRED whenever cell_size is given - snaps
    against the ACTUAL per-axis cell size block_mesh_dict builds
    (length / round(length/cell_size)), not the raw nominal cell_size -
    see mesh_gen._actual_axis_cell_size's docstring for why: whenever a
    room dimension isn't an exact multiple of cell_size (confirmed real:
    0.09m nominal on this room's dims), snapping against nominal instead
    of actual lands the carved zone's edges off every real mesh grid line,
    the same bug _opening_box had (fixed 2026-08-19) - a 0.8m source cube
    on a 0.09m mesh came out as 1000 real cells instead of the correctly-
    aligned 900.
    """
    cx, cy, cz = center
    if isinstance(size, (tuple, list)):
        sx, sy, sz = size
    else:
        sx = sy = sz = size
    lo = [cx - sx / 2, cy - sy / 2, cz - sz / 2]
    hi = [cx + sx / 2, cy + sy / 2, cz + sz / 2]
    if cell_size:
        if room_dims is None:
            raise ValueError("_source_box: room_dims is required when cell_size is given")
        cells = [_actual_axis_cell_size(room_dims[i], cell_size) for i in range(3)]
        for i in range(3):
            lo[i] = snap_outward(lo[i], cells[i], "lo")
            hi[i] = snap_outward(hi[i], cells[i], "hi")
            if hi[i] <= lo[i]:
                hi[i] = lo[i] + cells[i]
    return tuple(lo), tuple(hi)


def source_box_grid_alignment(center, size, cell_size, room_dims):
    """(nominal_size, actual_size) (width, height, depth) tuples for the
    source zone, comparing the raw requested box against what will
    actually be carved once snapped to cell_size - lets a caller warn
    about size drift BEFORE running a simulation (see
    run_pipeline.check_settings_grid_alignment), rather than discovering
    it after the fact the way check_ach_delivery does for the inlet.
    """
    lo_nom, hi_nom = _source_box(center, size)
    lo_snap, hi_snap = _source_box(center, size, cell_size=cell_size, room_dims=room_dims)
    nominal = tuple(h - l for l, h in zip(lo_nom, hi_nom))
    actual = tuple(h - l for l, h in zip(lo_snap, hi_snap))
    return nominal, actual


DEFAULT_SOURCE_ZONE_CELLS = 1


def source_size_from_cells(n_cells, cell_size, room_dims):
    """Per-axis (sx, sy, sz) size in METRES for a source zone of n_cells cells
    per axis.

    The zone size is configured in CELLS, not metres, because "one cell" is not
    one number: blockMesh divides each room dimension into round(L/cell_size)
    cells, so the ACTUAL cell size differs per axis whenever a dimension isn't
    a whole multiple of the nominal (a 2.57 m ceiling at a nominal 0.1 m builds
    26 cells of 0.098846 m). A single metre value therefore cannot mean "one
    cell" on every axis - 0.1 m is exactly 1 cell on a 3.2 m axis but 1.012
    cells on a 2.57 m one, which snaps outward to 2 cells and silently doubles
    the zone on that axis. Expressed in cells it is exact everywhere, and the
    box needs no snapping at all.

    Everything downstream still takes metres (and already accepts a per-axis
    tuple), so this is the only place the two representations meet.
    """
    n = max(1, int(round(n_cells)))
    return tuple(n * _actual_axis_cell_size(room_dims[i], cell_size) for i in range(3))


def resolve_source_zone_cells(settings, cell_size):
    """Source zone size in CELLS from a settings dict, migrating pre-2026-08-31
    projects that stored `source-zone-size` in metres.

    Old projects have no `source-zone-cells` key at all; their metre value is
    converted with the NOMINAL cell size (the actual per-axis sizes disagree,
    and any of them is as good an approximation as another for a one-time
    migration). Anything missing or unreadable falls back to the default rather
    than raising - an older .guvcfd must still open.
    """
    raw = settings.get("source-zone-cells")
    if raw is not None:
        try:
            return max(1, int(round(float(raw))))
        except (TypeError, ValueError):
            return DEFAULT_SOURCE_ZONE_CELLS
    legacy = settings.get("source-zone-size")
    if legacy is not None and cell_size:
        try:
            return max(1, int(round(float(legacy) / float(cell_size))))
        except (TypeError, ValueError, ZeroDivisionError):
            return DEFAULT_SOURCE_ZONE_CELLS
    return DEFAULT_SOURCE_ZONE_CELLS


def resolve_source_size(settings, cell_size, room_dims):
    """Per-axis (sx, sy, sz) source zone size in metres, from whichever of the
    cell-count or legacy metre setting the project has. The single entry point
    call sites should use instead of reading `source-zone-size` directly.
    """
    return source_size_from_cells(resolve_source_zone_cells(settings, cell_size), cell_size, room_dims)


def suggest_source_center_fix(center, size, cell_size, room_dims, fp_tol=1e-9):
    """(current, suggested) absolute (x, y, z) centers for the source zone -
    the 3D equivalent of mesh_gen.suggest_opening_center_fix, which does the
    same job for an inlet/outlet's two in-plane axes.

    Same lattice math, applied per axis: a box edge lands on a grid line
    exactly when (center - size/2) mod cell == 0, so the valid centers form
    a lattice spaced one cell apart, offset by phase = (size/2) mod cell.
    That phase matters - for an EVEN cell count the valid centers are grid
    lines, for an ODD count they sit at cell-CENTRE offsets instead, and a
    naive "round the centre to the nearest cell multiple" gets the odd case
    wrong. Ties break toward the room's midpoint on that axis so repeated
    snapping can't walk the source into a wall.

    Uses the ACTUAL per-axis cell size (_actual_axis_cell_size), not the
    nominal one, for the reason mesh_gen documents at length: whenever a room
    dimension isn't a whole multiple of cell_size, the real grid lines sit
    somewhere else entirely and snapping to nominal targets coordinates that
    match no real cell face.

    Only meaningful when `size` is already an exact multiple of the actual
    cell size on that axis - otherwise the two edges can never both land on
    the grid whatever centre is chosen. A one-cell source zone (the default)
    always satisfies this.

    Without this the source box is merely snapped OUTWARD (see _source_box),
    which keeps it grid-aligned but lets it grow up to a cell per axis;
    moving the centre instead keeps the requested size exactly.
    """
    cx, cy, cz = center
    if isinstance(size, (tuple, list)):
        sizes = tuple(size)
    else:
        sizes = (size, size, size)
    cur, sug = [], []
    for i, c in enumerate((cx, cy, cz)):
        cell = _actual_axis_cell_size(room_dims[i], cell_size)
        phase = _mod_phase(sizes[i] / 2, cell, fp_tol)
        cur.append(c)
        sug.append(_nearest_lattice_point(c, cell, phase, room_dims[i] / 2, fp_tol))
    return tuple(cur), tuple(sug)


def source_topo_set_dict(center, size, zone_name="sourceZone", cellset_name="sourceZoneCells", cell_size=None,
                          room_dims=None):
    """topoSetDict actions carving a small box cellZone (cellSet -> cellZoneSet,
    the standard two-step pattern) for the contaminant source. No faces/
    patches involved - this only tags cells, doesn't touch mesh topology.

    cell_size: if given, snap all 6 box edges to the mesh grid - see
    _source_box's docstring. room_dims required whenever cell_size is.
    """
    lo, hi = _source_box(center, size, cell_size=cell_size, room_dims=room_dims)

    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      topoSetDict;", "}", "",
        "actions", "(",
        "    {", f"        name    {cellset_name};", "        type    cellSet;",
        "        action  new;", "        source  boxToCell;",
        f"        box     ({lo[0]:.6g} {lo[1]:.6g} {lo[2]:.6g}) ({hi[0]:.6g} {hi[1]:.6g} {hi[2]:.6g});",
        "    }",
        "    {", f"        name    {zone_name};", "        type    cellZoneSet;",
        "        action  new;", "        source  setToCellZone;",
        f"        set     {cellset_name};", "    }",
        ");", "",
    ]
    return "\n".join(lines)


def write_source_topo_set_dict(case_dir, center, size, zone_name="sourceZone",
                                cellset_name="sourceZoneCells", filename="sourceTopoSetDict",
                                cell_size=None, room_dims=None):
    path = f"{case_dir}/system/{filename}"
    _write_case_file(case_dir, f"system/{filename}",
                      source_topo_set_dict(center, size, zone_name, cellset_name, cell_size=cell_size,
                                            room_dims=room_dims))
    return path


def compute_source_strength(room_volume, ventilation_ach, target_T_ss):
    """Total generation rate G [T-units * m^3 / s] such that, under the
    idealized well-mixed ODE (dT/dt = G/V - lambda_vent*T), the no-UV
    steady state would land at target_T_ss.

    The real CFD steady state will land near but not exactly at this value
    (imperfect mixing - same gap as well-mixed vs. effective eACH in the
    decay case). This sets a sensible starting magnitude for G, not a
    precisely-guaranteed target.
    """
    lambda_vent = ventilation_ach / 3600.0
    return room_volume * lambda_vent * target_T_ss


def source_Su(G_total, source_region_volume):
    """Volumetric injection rate Su [T-units/s] for the source cellZone."""
    return G_total / source_region_volume


def source_fvoptions_entry(Su, zone_name="sourceZone", field_name="T", entry_name="contaminantSource"):
    """fvOptions entry text for the always-on source: pure injection (Su
    constant, Sp=0 - not proportional to T, unlike the UV sink terms which
    are Su=0, Sp=-k).
    """
    lines = [
        f"{entry_name}",
        "{",
        "    type            scalarSemiImplicitSource;",
        "    active          true;",
        "",
        "    scalarSemiImplicitSourceCoeffs",
        "    {",
        "        selectionMode   cellZone;",
        f"        cellZone        {zone_name};",
        "        volumeMode      specific;",
        "",
        "        injectionRateSuSp",
        "        {",
        f"            {field_name}           ({Su:.6e} 0);",
        "        }",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


BREATHING_INLET_DEFAULT_DIRECTION = (0.0, 0.0, 1.0)
DEFAULT_BREATHING_VELOCITY = 0.06


def breathing_inlet_velocity(settings):
    """Exhale speed [m/s] from a project settings dict; 0 means no airflow.

    There is no separate enable flag - a velocity of 0 writes no constraint at
    all, which is exactly the old no-velocity behaviour. Missing or unreadable
    values fall back to the default so an older .guvcfd still opens.
    """
    try:
        v = float(settings.get("breathing-velocity", DEFAULT_BREATHING_VELOCITY))
    except (TypeError, ValueError):
        return DEFAULT_BREATHING_VELOCITY
    if v != v or abs(v) == float("inf") or v < 0:
        return DEFAULT_BREATHING_VELOCITY
    return v


def breathing_inlet_direction(settings):
    """(dx, dy, dz) exhale direction from an advanced-settings dict.

    Not cosmetic: pointing this at a vent short-circuits contaminant into the
    extract. With the source at (0.4, 1.2, 1.3) in patient ward 4B1 v7 an old
    +x default pointed straight into the 'outlet' patch (same y and z),
    dragging Phase 1's T_ss1 from ~1.13 down to 0.42 - see ANALYSIS_LOG.md
    2026-08-31. The default is now (0,0,1), straight up, which cannot line up
    with a wall-mounted opening.

    Tolerant of pre-2026-08-31 .guvcfd projects and hand-edited settings: the
    keys simply won't exist in an older file, and a hand-edit can leave a
    string/null/garbage value behind. Anything missing or non-numeric falls
    back to the default component rather than raising - a project saved before
    this field existed must still open and run.
    """
    def _f(key, fallback):
        try:
            v = float(settings.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return v if v == v and abs(v) != float("inf") else fallback  # reject NaN/inf

    dx, dy, dz = BREATHING_INLET_DEFAULT_DIRECTION
    d = (_f("breathing-dir-x", dx), _f("breathing-dir-y", dy), _f("breathing-dir-z", dz))
    # An all-zero vector has no direction to normalise; fall back rather than
    # emit a zero-velocity "constraint" that silently does nothing.
    return d if any(d) else BREATHING_INLET_DEFAULT_DIRECTION


def breathing_inlet_velocity_constraint(zone_name="sourceZone", entry_name="breathingInletVelocity",
                                        velocity_magnitude=0.06,
                                        direction=BREATHING_INLET_DEFAULT_DIRECTION):
    """fvOptions entry for the experimental breathing inlet: CONSTRAIN U to
    velocity_magnitude*direction inside the source cellZone (~0.06 m/s ~=
    resting tidal breathing), so the contaminant the volumetric T source
    injects there is carried by moving air instead of appearing in still air.

    A constraint, not a source, and the distinction is the whole point.
    An fvOption SOURCE adds terms to a cell's row of the A*x=b system (Su to
    the RHS, Sp to the diagonal) and leaves every other term in that row
    intact - so the value it "wants" is only one voice in the balance, and
    SIMPLE's pressure correction (U = HbyA - rAU*grad(p), which re-solves U
    to enforce continuity) can and does overrule it. A CONSTRAINT calls
    eqn.setValues(cells, value), which REPLACES the row with 1*U = value.
    No negotiation - the surrounding field adapts instead.

    That difference was measured, not assumed: breathing_inlet_momentum_source
    below (the superseded approach) balances at exactly 0.06 m/s on paper,
    yet a real Phase-1 run converged to 2.25 m/s in the zone - 37x the
    target, zone Courant ~22, TClampDecay firing every iteration. See
    ANALYSIS_LOG.md's 2026-08-30 entry.

    Still to verify on a real run (a constraint has its own failure mode -
    it prescribes a velocity the pressure field must remain consistent with):
    that zone |U| now actually reads velocity_magnitude, and that continuity
    /mass balance is unharmed. The zone is a through-flow (air in one face,
    out the other, no net mass added), so this should be satisfiable, but
    "should be" is not "was checked".
    """
    dir_norm = (direction[0]**2 + direction[1]**2 + direction[2]**2)**0.5
    if dir_norm == 0:
        direction = BREATHING_INLET_DEFAULT_DIRECTION
        dir_norm = 1.0
    else:
        direction = tuple(d / dir_norm for d in direction)
    vx, vy, vz = [velocity_magnitude * d for d in direction]

    lines = [
        f"{entry_name}",
        "{",
        "    type            vectorFixedValueConstraint;",
        "    active          true;",
        "",
        "    selectionMode   cellZone;",
        f"    cellZone        {zone_name};",
        "",
        "    fieldValues",
        "    {",
        f"        U               ({vx:.6e} {vy:.6e} {vz:.6e});",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


def breathing_inlet_momentum_source(zone_name="sourceZone", entry_name="breathingInletMomentum",
                                    velocity_magnitude=0.06,
                                    direction=BREATHING_INLET_DEFAULT_DIRECTION, sp_coeff=100.0):
    """SUPERSEDED by breathing_inlet_velocity_constraint - kept as a record of
    an approach that provably cannot do this job, not as a live option.

    Momentum source that drags U toward velocity_magnitude*direction within
    the source cellZone. OpenFOAM's SemiImplicitSource adds `Su + Sp*psi`, so
    relaxing toward a target is Su = sp_coeff*U_target with Sp = -sp_coeff,
    giving sp_coeff*(U_target - U) - a restoring drag. The Sp sign is NOT
    free: the same convention already applies to this module's UV sink terms
    (Su=0, Sp=-k, see source_fvoptions_entry's own docstring).

    Two things were wrong, discovered in that order:

    1. It originally shipped with Sp = +sp_coeff - positive feedback rather
       than drag, amplifying U instead of pulling it to the target. Measured
       at iteration 2000 of a real Phase-1 run: source-zone |U| was 21.6 m/s
       against a 0.06 m/s target (360x) at sp_coeff=100 and 2.6 m/s (43x) at
       sp_coeff=10, blowing whole-room mean velocity from the control's
       0.0147 m/s to 5.03 m/s.
    2. With the sign corrected it STILL converged to 2.25 m/s (37x target,
       zone Courant ~22, TClampDecay firing on every iteration) - because a
       source cannot dictate a velocity at all: it only adds terms to a row
       that SIMPLE's pressure correction then re-solves. Hence the constraint.

    Any result produced through this path is an artifact, not a breathing
    effect - see ANALYSIS_LOG.md's 2026-08-30 entry.
    """
    dir_norm = (direction[0]**2 + direction[1]**2 + direction[2]**2)**0.5
    if dir_norm == 0:
        direction = BREATHING_INLET_DEFAULT_DIRECTION
        dir_norm = 1.0
    else:
        direction = tuple(d / dir_norm for d in direction)
    vx, vy, vz = [velocity_magnitude * d for d in direction]

    lines = [
        f"{entry_name}",
        "{",
        "    type            vectorSemiImplicitSource;",
        "    active          true;",
        "",
        "    vectorSemiImplicitSourceCoeffs",
        "    {",
        "        selectionMode   cellZone;",
        f"        cellZone        {zone_name};",
        "        volumeMode      specific;",
        "",
        "        injectionRateSuSp",
        "        {",
        f"            U           (({vx*sp_coeff:.2e} {vy*sp_coeff:.2e} {vz*sp_coeff:.2e}) {-sp_coeff:.2e});",
        "        }",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


def write_fvoptions_file(case_dir, entries):
    """Write constant/fvOptions combining multiple pre-formatted entry text
    blocks (e.g. the contaminant source + UV cellZones from cellzones.py)
    into one file.
    """
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      fvOptions;", "}", "",
    ]
    lines.extend(entries)
    path = f"{case_dir}/constant/fvOptions"
    _write_case_file(case_dir, "constant/fvOptions", "\n".join(lines))
    return path


def _mass_balance_dict(patches):
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      massBalanceDict;", "}", "",
        "functions", "{",
        "    readFields1", "    {",
        "        type            readFields;",
        '        libs            ("libfieldFunctionObjects.so");',
        "        fields          (phi T);",
        "        executeControl  timeStep;",
        "        executeInterval 1;",
        "    }", "",
    ]
    for patch in patches:
        lines += [
            f"    {patch}FlowRate", "    {",
            "        type            surfaceFieldValue;",
            '        libs            ("libfieldFunctionObjects.so");',
            "        fields          (phi);",
            "        operation       sum;",
            "        regionType      patch;",
            f"        name            {patch};",
            "        executeControl  timeStep;",
            "        executeInterval 1;",
            "        writeControl    timeStep;",
            "        writeInterval   1;",
            "        writeFields     false;",
            "    }", "",
            f"    {patch}FlowWeightedT", "    {",
            "        type            surfaceFieldValue;",
            '        libs            ("libfieldFunctionObjects.so");',
            "        fields          (T);",
            "        operation       weightedAverage;",
            "        weightField     phi;",
            "        regionType      patch;",
            f"        name            {patch};",
            "        executeControl  timeStep;",
            "        executeInterval 1;",
            "        writeControl    timeStep;",
            "        writeInterval   1;",
            "        writeFields     false;",
            "    }", "",
        ]
    lines += ["}", ""]
    return "\n".join(lines)


def live_mass_balance_functions(patches, indent="    "):
    """Splice-ready controlDict `functions{}` entries (no FoamFile/functions
    wrapper - see monitoring.live_vol_average_functions, same pattern)
    tracking each patch's flow rate and flow-weighted T *every solver
    iteration*, instead of a single `postProcess -latestTime` snapshot
    (see check_mass_balance).

    Why this matters: a single-instant read is the wrong quantity to
    trust for "has mass balance actually converged" - confirmed directly
    on a real run where the true (windowed, many-iteration) ratio was a
    stable 89.9% (CV=0.475%, not noisy at all) but a naive derivative-
    based proxy computed from a short window gave a badly biased ~97-99%
    for the same period. The live per-iteration series this produces lets
    a caller compute a proper trailing-window average (see
    windowed_mass_balance) instead of trusting one snapshot.

    Named with a "Live" suffix, same collision-avoidance convention as
    every other live tracker spliced into this controlDict.
    """
    lines = []
    for patch in patches:
        lines += [
            "", f"{patch}FlowRateLive", "{",
            "    type            surfaceFieldValue;",
            '    libs            ("libfieldFunctionObjects.so");',
            "    fields          (phi);",
            "    operation       sum;",
            "    regionType      patch;",
            f"    name            {patch};",
            "    executeControl  timeStep;",
            "    executeInterval 1;",
            "    writeControl    timeStep;",
            "    writeInterval   1;",
            "    writeFields     false;",
            "}",
            "", f"{patch}FlowWeightedTLive", "{",
            "    type            surfaceFieldValue;",
            '    libs            ("libfieldFunctionObjects.so");',
            "    fields          (T);",
            "    operation       weightedAverage;",
            "    weightField     phi;",
            "    regionType      patch;",
            f"    name            {patch};",
            "    executeControl  timeStep;",
            "    executeInterval 1;",
            "    writeControl    timeStep;",
            "    writeInterval   1;",
            "    writeFields     false;",
            "}",
        ]
    return "\n".join(indent + line if line else "" for line in lines)


def windowed_mass_balance(t, flow_by_patch, weighted_t_by_patch, injection_rate_G, window_frac=0.15, tol=0.10):
    """Trailing-window mass-balance ratio from live per-iteration flux data
    (see live_mass_balance_functions), replacing check_mass_balance's
    single-instant snapshot with a proper windowed average - see that
    function's docstring for why a snapshot alone isn't trustworthy.

    flow_by_patch/weighted_t_by_patch: {patch_name: array} - the raw
    per-iteration sum(phi)/weightedAverage(T, weight=phi) series for each
    patch, same length as `t`.

    Returns the same shape as check_mass_balance() plus `cv` (the windowed
    ratio's own coefficient of variation) and `window_n`/`window_span` for
    labeling.

    cv is DETRENDED (residual after removing the window's systematic
    approach to steady state), not the raw spread around the mean - a
    window that's still climbing/falling has raw CV conflating that
    systematic drift with genuine noise, which can read as deceptively
    "tight" while the ratio is still moving several percent over the
    window (confirmed directly: a 15%-frac window still spanning a
    substantial change in mean gave a raw CV that understated how far from
    settled the ratio actually was).

    The ratio approaches 1.0 the same way T approaches Tinf - a single-
    exponential relaxation, not a straight line (both come from the same
    underlying SIMPLE-iteration convergence) - so the trend removed here is
    fit_asymptotic_value's exponential model, not a linear one; a linear
    detrend still leaves real curvature in the residual whenever the window
    hasn't fully flattened, understating drift the same way raw CV does.
    Falls back to a linear detrend if the exponential fit doesn't converge
    (see fit_asymptotic_value's own self-consistency guard), and further to
    the plain mean-relative std for n<=2 or if even that's degenerate.
    """
    import numpy as np
    t = np.asarray(t, dtype=float)
    n = max(2, round(len(t) * window_frac))
    removal_rate = np.zeros(n)
    for patch in flow_by_patch:
        phi = np.asarray(flow_by_patch[patch][-n:], dtype=float)
        wT = np.asarray(weighted_t_by_patch[patch][-n:], dtype=float)
        removal_rate += np.abs(phi) * wT
    ratio_series = removal_rate / injection_rate_G if injection_rate_G else np.full(n, float("inf"))
    ratio = float(ratio_series.mean())
    tail_t = t[-n:]
    cv = None
    if ratio and n >= 10:
        fit = fit_asymptotic_value(tail_t, ratio_series, fit_frac=1.0)
        if fit is not None:
            cv = fit["fit_cv"]
    if cv is None and ratio and n > 2:
        slope, intercept = np.polyfit(tail_t, ratio_series, 1)
        residuals = ratio_series - (slope * tail_t + intercept)
        cv = float(residuals.std(ddof=2) / ratio)
    elif cv is None and ratio:
        cv = float(ratio_series.std(ddof=1) / ratio)
    within_tolerance = (1 - tol) <= ratio <= (1 + tol)
    window_span = float(t[-1] - t[-n])
    return {
        "measured_removal_rate": float(removal_rate.mean()), "injection_rate": injection_rate_G,
        "ratio": ratio, "within_tolerance": within_tolerance, "tol": tol,
        "cv": cv, "window_n": n, "window_span": window_span,
    }


def check_mass_balance(case_dir, patches, injection_rate_G, tol=0.10, log_fn=print):
    """Compare the actual contaminant removal rate leaving through
    `patches` (flow-weighted mean T times the outlet flow rate, summed
    across every patch given) against the known injection rate `G` this
    phase's source was configured with - a convergence check that needs no
    curve-fitting or windowing assumptions at all: at true steady state,
    injection must equal removal exactly (T's own volume integral has
    stopped changing), so any gap between them *is* the current
    accumulation rate, direction and magnitude both. Complements (doesn't
    replace) the windowed-CV/T-infinity checks - confirmed directly on a
    real, not-yet-converged Phase 1 run: outlet removal read ~0.0201
    against G=0.027 while T was still visibly climbing, tracking the still-
    open gap exactly.

    Only meaningful for Phase 1 (source, no UV) - Phase 2 also removes T via
    the UV sink cellZones themselves (a volumetric loss, not just advective
    outflow), so injection = outlet removal alone no longer holds there;
    doing the equivalent check for Phase 2 would additionally need the
    integrated UV sink rate across every uvZone cellZone, not just the
    outlet patches.

    Returns a dict: {measured_removal_rate, injection_rate, ratio,
    within_tolerance, tol}. ratio = measured_removal_rate / injection_rate.
    """
    case_dir_wsl = wsl_path(case_dir)
    dict_path = f"{case_dir}/system/massBalanceDict"
    _write_case_file(case_dir, "system/massBalanceDict", _mass_balance_dict(patches))

    r = run_wsl_or_raise("postProcess -dict system/massBalanceDict -latestTime", case_dir_wsl,
                          "measuring mass balance (outlet removal vs injection)")
    run_wsl("rm -rf postProcessing", case_dir_wsl)

    measured_removal_rate = 0.0
    for patch in patches:
        flow_m = re.search(rf"sum\({patch}\) of phi = ([\-0-9.eE+]+)", r.stdout)
        t_m = re.search(rf"weightedAverage\({patch}\) of T = ([\-0-9.eE+]+)", r.stdout)
        if not flow_m or not t_m:
            raise RuntimeError(
                f"Could not parse flow rate/flow-weighted T for patch {patch!r} from "
                f"postProcess output:\n{r.stdout}")
        measured_removal_rate += abs(float(flow_m.group(1))) * float(t_m.group(1))

    ratio = measured_removal_rate / injection_rate_G if injection_rate_G else float("inf")
    within_tolerance = (1 - tol) <= ratio <= (1 + tol)

    if within_tolerance:
        log_fn(f"Mass balance check: outlet removal {measured_removal_rate:.4g} vs injection "
               f"{injection_rate_G:.4g} (ratio {ratio:.2%}) - within +/-{tol:.0%}, consistent with "
               f"a converged steady state.")
    else:
        log_fn(f"Mass balance check: outlet removal {measured_removal_rate:.4g} vs injection "
               f"{injection_rate_G:.4g} (ratio {ratio:.2%}) - outside +/-{tol:.0%}. T is still "
               f"accumulating (or losing) faster than this tolerance allows - the reported T_ss "
               f"may not reflect the true steady state yet.")

    return {
        "measured_removal_rate": measured_removal_rate, "injection_rate": injection_rate_G,
        "ratio": ratio, "within_tolerance": within_tolerance, "tol": tol,
    }
