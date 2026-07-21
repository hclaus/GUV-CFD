"""Continuous contaminant source for steady-state scenarios: a small,
always-on cellZone with a positive scalarSemiImplicitSource, representing
e.g. a continuously-shedding occupant (Wells-Riley-style continuous point
source). No mesh/patch changes needed - a topoSet-carved cellZone, distinct
from (and coexisting with) the UV sink cellZones in cellzones.py.

Two phases share this source, staying on throughout:
  Phase 1 (no UV): T starts warm (see steady_state_pipeline.run_steady_state_
    scenario's T_initial=target_T_ss), builds up/settles to a steady state
    set by the balance between this source and ventilation removal alone.
  Phase 2 (UV on): starting from phase 1's converged T, UV cellZones are
    added on top of the still-active source, reaching a new, lower steady
    state.
"""
import re

from .wsl_utils import wsl_path, run_wsl_or_raise, run_wsl


def source_topo_set_dict(center, size, zone_name="sourceZone", cellset_name="sourceZoneCells", cell_size=None):
    """topoSetDict actions carving a small box cellZone (cellSet -> cellZoneSet,
    the standard two-step pattern) for the contaminant source. No faces/
    patches involved - this only tags cells, doesn't touch mesh topology.

    cell_size: if given, snap all 6 box edges to the nearest mesh grid
    line - see mesh_gen._opening_box's docstring for why this matters (a
    center/size combination that doesn't land on a whole number of cells
    puts the raw box edges right on a boxToCell floating-point boundary
    tie, producing an inconsistent/asymmetric carved zone instead of a
    clean, deterministic block).
    """
    cx, cy, cz = center
    if isinstance(size, (tuple, list)):
        sx, sy, sz = size
    else:
        sx = sy = sz = size
    lo = [cx - sx / 2, cy - sy / 2, cz - sz / 2]
    hi = [cx + sx / 2, cy + sy / 2, cz + sz / 2]
    if cell_size:
        for i in range(3):
            lo[i] = round(lo[i] / cell_size) * cell_size
            hi[i] = round(hi[i] / cell_size) * cell_size
            if hi[i] <= lo[i]:
                hi[i] = lo[i] + cell_size

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
                                cell_size=None):
    path = f"{case_dir}/system/{filename}"
    with open(path, "w") as f:
        f.write(source_topo_set_dict(center, size, zone_name, cellset_name, cell_size=cell_size))
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
    with open(path, "w") as f:
        f.write("\n".join(lines))
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
    with open(dict_path, "w") as f:
        f.write(_mass_balance_dict(patches))

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
