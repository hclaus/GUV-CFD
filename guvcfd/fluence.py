"""Compute fluence rate at an arbitrary point cloud (e.g. CFD mesh cell centers).

guv_calcs doesn't expose a public "fluence at these points" function - every
CalcZone (Plane/Volume/Point) works by building a ZoneView (a coordinate
array plus a few flags) and handing it to LightingCalculator. Nothing about
that requires the coordinates to come from a structured grid, so we build a
ZoneView directly from the CFD mesh's cell centers and call the same
compute()/compute_reflectance() that CalcVol.calculate_values() uses
internally. calctype="volume" skips all FOV/plane-angle filtering, giving
plain isotropic fluence rate - the same physics as a "Whole Room Fluence" zone.
"""
import numpy as np
from guv_calcs.calc_manager import LightingCalculator
from guv_calcs.calc_zone._zone import ZoneView


def compute_fluence_at_points(room, points, zone_id="openfoam_cellcenters"):
    """Compute fluence rate (room's native intensity units, e.g. uW/cm^2) at each point.

    points: (N, 3) array of coordinates in the room's own units (meters).
    Mirrors Room.calculate()'s per-zone computation exactly.
    """
    points = np.asarray(points, dtype=float)
    n = len(points)

    valid_lamps = room.lamps.valid()
    all_surfs = room.all_surfaces

    if room.recalculate_incidence:
        room.ref_manager.calculate_incidence(valid_lamps, all_surfs, hard=True)

    zv = ZoneView(
        zone_id=zone_id,
        coords=points,
        num_points=(n,),
        calc_state=(zone_id, n),
        update_state=(zone_id, n),
        calctype="volume",
    )

    calculator = LightingCalculator()
    base_values = calculator.compute(
        lamps=valid_lamps, zv=zv, surfaces=all_surfs,
        enable_occlusion=True, hard=True,
    )

    if all_surfs and room.ref_manager.enabled:
        reflected_values = calculator.compute_reflectance(
            surfaces=all_surfs, zv=zv, enable_occlusion=True, hard=True,
        )
        values = base_values + reflected_values
    else:
        values = base_values

    return np.asarray(values, dtype=float).reshape(n)


def compute_inactivation_rate(fluence_uW_cm2, Z):
    """UV inactivation rate constant k [1/s] = Z [cm^2/mJ] * E [uW/cm^2] * 1e-3.

    Same formula as map_fluence_to_mesh.py's k_values calculation. The 1e-3
    factor converts uW/cm^2 to mJ/(cm^2*s) so Z (cm^2/mJ) * E (mJ/(cm^2*s))
    comes out in 1/s.
    """
    return Z * np.asarray(fluence_uW_cm2, dtype=float) * 1e-3


def compute_well_mixed_eACH(k_per_s):
    """Well-mixed eACH_UV [1/hr] = k [1/s] * 3600, i.e. Z * E_avg * 3.6.

    The standard way UV disinfection performance gets communicated in the
    GUV literature - expresses the UV-driven decay rate in the same units
    as ventilation ACH, so the two are directly comparable/additive
    (total effective ACH = ventilation ACH + eACH_UV). "Well-mixed" because
    it assumes perfect instantaneous mixing (every point in the room gets
    the same UV dose as the volume average) - an upper bound. Compare
    against decay_analysis.compute_effective_eACH(), which fits the actual
    CFD decay curve instead of assuming perfect mixing.
    """
    return np.asarray(k_per_s, dtype=float) * 3600.0
