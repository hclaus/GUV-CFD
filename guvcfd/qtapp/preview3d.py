"""Live 3D room/lamp/inlet/outlet/fan/source/monitoring preview - the
native (PyVista, GPU-accelerated, smoothly interactive) replacement for
guvcfd.visualization's Plotly-based preview, which can't be embedded
natively in Qt. Reuses mesh_gen.py/initial_fields.py's pure geometry
helpers for opening/wall placement so the preview matches what
setup_case() will actually carve - see guvcfd.visualization.plot_case for
the reference feature set this mirrors.
"""
import numpy as np
import pyvista as pv
from pyvistaqt import QtInteractor
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..initial_fields import WALL_INFLOW_DIRECTION
from ..mesh_gen import _WALL_SPECS
from ..mesh_gen import opening_center as _opening_center
from ..visualization import _rect_outline

_WALL_LABEL_POSITIONS = {
    "xMin": lambda Lx, Ly, Lz: (0, Ly / 2, Lz / 2),
    "xMax": lambda Lx, Ly, Lz: (Lx, Ly / 2, Lz / 2),
    "frontWall": lambda Lx, Ly, Lz: (Lx / 2, 0, Lz / 2),
    "backWall": lambda Lx, Ly, Lz: (Lx / 2, Ly, Lz / 2),
    "floor": lambda Lx, Ly, Lz: (Lx / 2, Ly / 2, 0),
    "ceiling": lambda Lx, Ly, Lz: (Lx / 2, Ly / 2, Lz),
}


def _num(value, default=0.0):
    """A settings value as a float, tolerating None/blank/garbage - preview
    code must never raise on a half-typed field."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _breathing_velocity(settings):
    return max(_num(settings.get("breathing-velocity"), 0.0), 0.0)


class Preview3D(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # auto_update=False: QtInteractor otherwise defaults to a
        # BACKGROUND QTimer that force-calls render() every 200ms forever
        # (auto_update=5.0 Hz by default) - meant to catch scene changes
        # that don't trigger VTK's own Modified() signal, but we always
        # call render() ourselves explicitly at the end of update_scene()
        # already, so it's pure unwanted continuous re-rendering here -
        # the likely cause of "flickering for ~10s" after every load
        # (confirmed independently: it also starves the Qt event loop
        # badly enough in this environment that even an unrelated
        # QTimer.singleShot() failed to fire at all with it left on).
        self.plotter = QtInteractor(self, auto_update=False)
        layout.addWidget(self.plotter.interactor)
        self.plotter.set_background("white")

    def update_scene(self, room, settings):
        """room: a guv_calcs Room (x/y/z/units/lamps). settings: the
        Project Setup tab's current field values (dict, same shape as
        run_pipeline.setup_case()'s own kwargs, via the helpers module)."""
        # Preserve the user's current camera framing across live edits
        # (typing in a field shouldn't reset zoom/rotation) - only the
        # very first render (no camera set yet) gets an automatic reset.
        had_camera = bool(self.plotter.renderer.camera_set)
        self.plotter.clear()
        if room is None:
            self.plotter.render()
            return

        Lx, Ly, Lz = room.x, room.y, room.z
        box = pv.Box(bounds=(0, Lx, 0, Ly, 0, Lz))
        self.plotter.add_mesh(box, style="wireframe", color="black", line_width=1.5)
        floor = pv.Plane(center=(Lx / 2, Ly / 2, 0), direction=(0, 0, 1), i_size=Lx, j_size=Ly)
        self.plotter.add_mesh(floor, color="lightgray", opacity=0.2)
        for wall, fn in _WALL_LABEL_POSITIONS.items():
            self.plotter.add_point_labels(
                [fn(Lx, Ly, Lz)], [wall], point_size=0, font_size=14, text_color="#444444",
                shape=None, always_visible=True, show_points=False)

        for lamp_id in room.lamps.valid():
            lamp = room.lamps[lamp_id]
            pos = (lamp.x, lamp.y, lamp.z)
            self.plotter.add_mesh(pv.Sphere(radius=max(Lx, Ly, Lz) * 0.015, center=pos), color="gold")
            aim = (lamp.aimx, lamp.aimy, lamp.aimz)
            if aim != pos:
                self.plotter.add_lines(np.array([pos, aim]), color="goldenrod", width=2)

        # Primary inlet/outlet are mandatory, so always drawn - the old
        # "-show" flag was preview-only and has been removed from the tab.
        # An older .guvcfd may still carry inlet-show/outlet-show; it is
        # ignored rather than honoured, so the two openings that always
        # exist in the mesh are always visible.
        self._add_opening(room, settings, "inlet", "Inlet", "#2ecc71", is_inlet=True)
        self._add_opening(room, settings, "outlet", "Outlet", "#e74c3c", is_inlet=False)
        if settings.get("inlet2-enable"):
            self._add_opening(room, settings, "inlet2", "Inlet 2", "#2ecc71", is_inlet=True)
        if settings.get("outlet2-enable"):
            self._add_opening(room, settings, "outlet2", "Outlet 2", "#e74c3c", is_inlet=False)
        if settings.get("fan-enable"):
            self._add_fan(settings)
        # Draw the injection/source zone whenever it actually exists, not only
        # in steady-state. Decay is the DEFAULT mode, and a decay run carves
        # the same zone whenever the breathing velocity is non-zero (see
        # scenario_runs._carve_breathing_inlet) - so gating on sim-type alone
        # left it invisible in the mode most runs use.
        if settings.get("sim-type") == "steady_state" or _breathing_velocity(settings) > 0:
            self._add_injection(settings)
        if settings.get("monitoring-enable"):
            self._add_monitoring_points(settings)

        if had_camera:
            self.plotter.render()
        else:
            self._set_initial_camera(Lx, Ly, Lz)

    def _set_initial_camera(self, Lx, Ly, Lz):
        # Same framing guvcfd.visualization.plot_case uses: view from the
        # -x/-y octant, so the (0,0) origin corner faces the viewer instead
        # of hiding around the back. Position/focal_point/up set the VIEW
        # DIRECTION first; reset_camera() must come AFTER - it fits the
        # scene bounds by moving the camera along whatever direction is
        # already set, so calling it first (the original order here) fit
        # the bounds for the PREVIOUS/default direction and then the
        # explicit eye position below threw that framing away, leaving the
        # view too close/cropped (confirmed via a real screenshot).
        center = (Lx / 2, Ly / 2, Lz / 2)
        diag = (Lx ** 2 + Ly ** 2 + Lz ** 2) ** 0.5
        eye = (center[0] - diag * 0.7, center[1] - diag * 0.7, center[2] + diag * 0.55)
        self.plotter.camera.position = eye
        self.plotter.camera.focal_point = center
        self.plotter.camera.up = (0, 0, 1)
        self.plotter.reset_camera()
        self.plotter.render()

    def _add_opening(self, room, settings, prefix, label, color, is_inlet):
        wall = settings.get(f"{prefix}-wall")
        if wall not in _WALL_SPECS:
            return
        try:
            from . import helpers
            c1, c2 = helpers.opening_center_frac(settings, prefix, room)
            size = (settings[f"{prefix}-size-w"], settings[f"{prefix}-size-h"])
            center = _opening_center(wall, room.x, room.y, room.z, (c1, c2), size)
        except (KeyError, ZeroDivisionError, TypeError):
            return
        xs, ys, zs = _rect_outline(center, wall, size)
        poly = pv.MultipleLines(points=np.column_stack([xs, ys, zs]))
        self.plotter.add_mesh(poly, color=color, line_width=4)
        normal = np.array(WALL_INFLOW_DIRECTION[wall], dtype=float)
        if not is_inlet:
            normal = -normal  # outlet: arrow shows air LEAVING, not the wall's inward normal
        arrow_len = min(room.x, room.y, room.z) * 0.18
        tip = np.array(center) + normal * arrow_len
        self.plotter.add_lines(np.array([center, tip]), color=color, width=3)
        self.plotter.add_point_labels(
            [tuple(tip)], [label], point_size=0, font_size=12, text_color=color,
            shape=None, always_visible=True, show_points=False)

    def _add_fan(self, settings):
        try:
            center = (settings["fan-x-input"], settings["fan-y-input"], settings["fan-z-input"])
            radius = settings["fan-radius"]
            thickness = settings.get("fan-thickness", 0.2)
        except (KeyError, TypeError):
            return
        direction = (0, 0, -1) if settings.get("fan-direction", "down") == "down" else (0, 0, 1)
        cyl = pv.Cylinder(center=center, direction=direction, radius=radius, height=thickness,
                           resolution=32, capping=True)
        self.plotter.add_mesh(cyl, color="mediumpurple", opacity=0.35)
        self.plotter.add_mesh(cyl.extract_feature_edges(), color="mediumpurple", line_width=2)
        d = np.array(direction, dtype=float)
        d /= np.linalg.norm(d)
        tip = np.array(center) + d * radius * 1.3
        self.plotter.add_lines(np.array([center, tip]), color="mediumpurple", width=3)
        self.plotter.add_point_labels(
            [tuple(np.array(center) + (0, 0, radius + 0.15))], ["Fan"], point_size=0, font_size=12,
            text_color="mediumpurple", shape=None, always_visible=True, show_points=False)

    def _add_injection(self, settings):
        try:
            center = (settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"])
        except (KeyError, TypeError):
            return
        # Sized from the real zone setting rather than a fixed radius, and
        # drawn as a cube like the monitoring points, so its actual extent is
        # visible instead of a dot that reads as "nothing is there".
        cells = settings.get("source-zone-cells") or 1
        try:
            size = max(float(cells), 1.0) * 0.1     # preview-only cell-size approximation
        except (TypeError, ValueError):
            size = 0.1
        box = pv.Cube(center=center, x_length=size, y_length=size, z_length=size)
        self.plotter.add_mesh(box, color="#9b59b6", opacity=0.55)
        self.plotter.add_mesh(pv.Cube(center=center, x_length=size, y_length=size, z_length=size),
                               style="wireframe", color="#6c3483", line_width=2)

        # The jet direction is a real setting now, and pointing it the wrong
        # way changed T_ss by 4.5x - so show it rather than leave the user to
        # infer it from three numbers in a form.
        v = _breathing_velocity(settings)
        label = "Injection"
        if v > 0:
            d = (_num(settings.get("breathing-dir-x"), 0.0),
                 _num(settings.get("breathing-dir-y"), 0.0),
                 _num(settings.get("breathing-dir-z"), 1.0))
            mag = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
            if mag > 0:
                arrow_len = max(size * 3.0, 0.35)
                self.plotter.add_mesh(
                    pv.Arrow(start=center, direction=d, scale=arrow_len,
                             tip_length=0.3, tip_radius=0.11, shaft_radius=0.035),
                    color="#9b59b6")
            label = f"Injection  {v:g} m/s"
        self.plotter.add_point_labels(
            [(center[0], center[1], center[2] + size / 2 + 0.12)], [label],
            point_size=0, font_size=12, text_color="#6c3483",
            shape=None, always_visible=True, show_points=False)

    def _add_monitoring_points(self, settings):
        cell_size = 0.1  # preview-only approximation of the real mesh cell size (advanced setting)
        for i in (1, 2, 3):
            if not settings.get(f"monitor{i}-enable"):
                continue
            try:
                center = (settings[f"monitor{i}-x-input"], settings[f"monitor{i}-y-input"],
                          settings[f"monitor{i}-z-input"])
                size = settings[f"monitor{i}-cells"] * cell_size
            except (KeyError, TypeError):
                continue
            name = settings.get(f"monitor{i}-name") or f"Point {i}"
            box = pv.Cube(center=center, x_length=size, y_length=size, z_length=size)
            self.plotter.add_mesh(box, color="#ff1493", opacity=0.3)
            self.plotter.add_point_labels(
                [(center[0], center[1], center[2] + size / 2 + 0.1)], [name], point_size=0, font_size=12,
                text_color="#ff1493", shape=None, always_visible=True, show_points=False)
