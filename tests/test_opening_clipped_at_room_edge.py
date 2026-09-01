"""An opening flush against a room edge must not report more area than the
mesh can carve.

Regression for patient ward 4B1 v10, which delivered 4.78 /hr against a
nominal 6 /hr with no error: the inlet's outward grid snap pushed its top
edge above the ceiling, blockMesh carved 4 rows instead of 5, and the inlet
velocity had been sized as ACH*V/area on the un-clipped 5-row area - so the
room received exactly 4/5 of its intended ventilation.
"""
from collections import namedtuple

from guvcfd.mesh_gen import _opening_box, opening_actual_area, _actual_axis_cell_size
from guvcfd.visualization import center_frac_for_wall

Room = namedtuple("Room", "x y z")
# The real v10 geometry. 2.57 m is NOT a whole multiple of the 0.1 m nominal
# cell, so the true dz is 2.57/26 = 0.0988462 - which is what makes the snap
# overshoot in the first place.
ROOM = Room(3.2, 4.8, 2.57)
CELL = 0.1
SIZE = (0.6, 0.4)


def _area(y, z):
    cf = center_frac_for_wall("xMax", y, z, ROOM)
    return opening_actual_area("xMax", ROOM.x, ROOM.y, ROOM.z, cf, SIZE, cell_size=CELL)


def _box_z(y, z):
    cf = center_frac_for_wall("xMax", y, z, ROOM)
    lo, hi = _opening_box("xMax", ROOM.x, ROOM.y, ROOM.z, cf, SIZE, cell_size=CELL)
    return lo[2], hi[2]


def test_opening_box_never_extends_past_the_room():
    """The inlet at z=2.4 snaps upward to 2.6688 before clipping - 0.0988 m
    above a 2.57 m ceiling."""
    lo, hi = _box_z(1.2, 2.4)
    assert hi <= ROOM.z + 1e-9, f"box top {hi} is above the {ROOM.z} ceiling"
    assert lo >= -1e-9


def test_clipped_inlet_area_matches_what_the_mesh_carves():
    """Mesh reality: 24 faces = 6 cells wide x 4 rows tall."""
    dz = _actual_axis_cell_size(ROOM.z, CELL)
    assert _area(1.2, 2.4) == __import__("pytest").approx(0.6 * 4 * dz)


def test_unclipped_outlet_is_left_alone():
    """The outlet at z=1.3 is nowhere near an edge, so it keeps the full
    outward-snapped 5 rows (30 faces) - the fix must not shrink it."""
    dz = _actual_axis_cell_size(ROOM.z, CELL)
    assert _area(1.2, 1.3) == __import__("pytest").approx(0.6 * 5 * dz)


def test_delivered_ach_is_now_nominal():
    """End to end: velocity sized on the reported area, applied across the
    carved area, must deliver the requested ACH. Was 4.80 against 6."""
    volume = ROOM.x * ROOM.y * ROOM.z
    nominal_q = 6.0 * volume / 3600.0
    inlet_area = _area(1.2, 2.4) + _area(3.6, 2.4)   # two inlets
    v_mag = nominal_q / inlet_area
    dz = _actual_axis_cell_size(ROOM.z, CELL)
    carved_area = 2 * 0.6 * 4 * dz                    # what the mesh really has
    delivered_ach = v_mag * carved_area * 3600.0 / volume
    assert delivered_ach == __import__("pytest").approx(6.0, rel=1e-9)
