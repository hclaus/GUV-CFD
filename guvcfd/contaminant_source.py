"""Continuous contaminant source for steady-state scenarios: a small,
always-on cellZone with a positive scalarSemiImplicitSource, representing
e.g. a continuously-shedding occupant (Wells-Riley-style continuous point
source). No mesh/patch changes needed - a topoSet-carved cellZone, distinct
from (and coexisting with) the UV sink cellZones in cellzones.py.

Two phases share this source, staying on throughout:
  Phase 1 (no UV): T starts at 0, builds up to a steady state set by the
    balance between this source and ventilation removal alone.
  Phase 2 (UV on): starting from phase 1's converged T, UV cellZones are
    added on top of the still-active source, reaching a new, lower steady
    state.
"""


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
