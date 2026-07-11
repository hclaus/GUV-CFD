"""Bin a continuous per-cell UV inactivation rate into piecewise-uniform
cellZones + fvOptions sink terms, since fvOptions' scalarSemiImplicitSource
takes a uniform coefficient per cellZone - it can't consume a spatially
varying field directly. Ported from map_fluence_to_mesh.py (bin_decay_rates,
write_cellzones, write_fvoptions), which was already validated against a
well-mixed-room comparison (cfd_vs_wellmixed.png). Only the input changed:
here k_values comes from direct per-cell computation (fluence.py), not
CSV/interpolation.
"""
import numpy as np


def bin_decay_rates(k_values, nbins):
    """Bin per-cell decay rates into nbins log-spaced groups (plus a zero bin).

    Returns (bin_idx, bin_repr): bin_idx[i] is the bin index (0 = zero-rate
    "no source" bin) for cell i; bin_repr[b] is the representative decay rate
    (geometric mean of the bin's [lo, hi) edges) for bin b.
    """
    k_pos = k_values[k_values > 0]
    if len(k_pos) == 0:
        raise RuntimeError("All decay rates are zero - check fluence data / Z factor")
    k_min = k_pos.min()
    k_max = k_pos.max()
    edges = np.logspace(np.log10(k_min), np.log10(k_max), nbins + 1)

    bin_idx = np.zeros(len(k_values), dtype=int)  # 0 = "zero" bin
    bin_repr = [0.0]  # representative k for bin 0 is 0 (no source added there)

    for i, kv in enumerate(k_values):
        if kv <= 0:
            bin_idx[i] = 0
        else:
            b = np.searchsorted(edges, kv, side='right') - 1
            b = min(max(b, 0), nbins - 1)
            bin_idx[i] = b + 1  # shift by 1 since 0 is reserved for "zero"

    for b in range(nbins):
        lo, hi = edges[b], edges[b + 1]
        bin_repr.append(np.sqrt(lo * hi))  # geometric mean as representative value

    return bin_idx, bin_repr


def write_cellzones(case_dir, bin_idx, nbins):
    """Write constant/polyMesh/cellZones. Returns the list of zone names."""
    n_cells = len(bin_idx)
    lines = [
        "FoamFile",
        "{",
        "    version     2.0;",
        "    format      ascii;",
        "    class       regIOobject;",
        '    location    "constant/polyMesh";',
        "    object      cellZones;",
        "}",
        "",
    ]

    zone_names = [f"uvZone{b}" for b in range(nbins + 1)]  # +1 for the zero bin (uvZone0)
    lines.append(f"{len(zone_names)}")
    lines.append("(")
    for b, name in enumerate(zone_names):
        cell_ids = np.where(bin_idx == b)[0]
        lines.append(f"    {name}")
        lines.append("    {")
        lines.append("        type cellZone;")
        lines.append(f"        cellLabels      List<label> {len(cell_ids)}")
        lines.append("(")
        lines.append(" ".join(str(c) for c in cell_ids))
        lines.append(")")
        lines.append("        ;")
        lines.append("    }")
    lines.append(")")
    lines.append("")

    path = f"{case_dir}/constant/polyMesh/cellZones"
    with open(path, "w") as f:
        f.write("\n".join(lines))

    return zone_names, path


def write_fvoptions(case_dir, zone_names, bin_repr, field_name="T"):
    """Write constant/fvOptions: one scalarSemiImplicitSource per non-zero zone,
    applying -k * <field_name> as a sink term (UV inactivation of the transported
    scalar).
    """
    lines = [
        "FoamFile",
        "{",
        "    version     2.0;",
        "    format      ascii;",
        "    class       dictionary;",
        "    object      fvOptions;",
        "}",
        "",
    ]

    for b, name in enumerate(zone_names):
        k = bin_repr[b]
        if k <= 0:
            continue  # skip zero-decay zone, no source needed
        lines.append(f"uvSource_{name}")
        lines.append("{")
        lines.append("    type            scalarSemiImplicitSource;")
        lines.append("    active          true;")
        lines.append("")
        lines.append("    scalarSemiImplicitSourceCoeffs")
        lines.append("    {")
        lines.append("        selectionMode   cellZone;")
        lines.append(f"        cellZone        {name};")
        lines.append("        volumeMode      specific;")
        lines.append("")
        lines.append("        injectionRateSuSp")
        lines.append("        {")
        lines.append(f"            {field_name}           (0 {-k:.6e});")
        lines.append("        }")
        lines.append("    }")
        lines.append("}")
        lines.append("")

    path = f"{case_dir}/constant/fvOptions"
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path
