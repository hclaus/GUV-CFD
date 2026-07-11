"""3D preview of a case's OpenFOAM-relevant setup - room + lamps (via
guv_calcs.RoomPlotter, reused as-is: lamps render as their actual
photometric web mesh plus an aim-direction line, not just a placeholder
arrow) extended with inlet/outlet openings, an optional mixing fan, and
wall labels (so it's clear which wall is which without cross-referencing
the mesh dict).

Takes the same parameters as run_pipeline.setup_case() - meant for
previewing a configuration *before* running mesh generation/OpenFOAM, not
for reading back an already-built case.
"""
import numpy as np
import plotly.graph_objs as go
from guv_calcs.room_plotter import RoomPlotter

from .mesh_gen import _opening_box

_WALL_LABEL_POSITIONS = {
    # wall name -> (position, room-fraction basis)
    "xMinWall": lambda Lx, Ly, Lz: (0, Ly / 2, Lz / 2),
    "xMaxWall": lambda Lx, Ly, Lz: (Lx, Ly / 2, Lz / 2),
    "frontWall": lambda Lx, Ly, Lz: (Lx / 2, 0, Lz / 2),
    "backWall": lambda Lx, Ly, Lz: (Lx / 2, Ly, Lz / 2),
    "floor": lambda Lx, Ly, Lz: (Lx / 2, Ly / 2, 0),
    "ceiling": lambda Lx, Ly, Lz: (Lx / 2, Ly / 2, Lz),
}

# Direction air moves through the room, wall the opening sits on -> unit vector.
# Used for both inlet (flow entering) and outlet (flow continuing/exiting) arrows,
# so both point the same way - reads as "this is the direction flow moves".
_WALL_FLOW_DIRECTION = {"xMin": (1, 0, 0), "xMax": (1, 0, 0)}


def _add_wall_labels(fig, Lx, Ly, Lz):
    xs, ys, zs, texts = [], [], [], []
    for name, fn in _WALL_LABEL_POSITIONS.items():
        x, y, z = fn(Lx, Ly, Lz)
        xs.append(x)
        ys.append(y)
        zs.append(z)
        texts.append(name)
    fig.add_trace(go.Scatter3d(
        x=xs, y=ys, z=zs, mode="text", text=texts,
        textfont=dict(size=13, color="#444444"),
        name="Wall labels", customdata=["wall_labels"], showlegend=False,
    ))
    return fig


def _rect_outline(center, wall, size):
    """(x,y,z) outline of a rectangular opening on an xMin/xMax wall."""
    cx, cy, cz = center
    w, h = size
    corners_yz = [(cy - w / 2, cz - h / 2), (cy + w / 2, cz - h / 2),
                  (cy + w / 2, cz + h / 2), (cy - w / 2, cz + h / 2),
                  (cy - w / 2, cz - h / 2)]
    x = [cx] * 5
    y = [c[0] for c in corners_yz]
    z = [c[1] for c in corners_yz]
    return x, y, z


def _add_opening(fig, label, wall, center_frac, size, Lx, Ly, Lz, color):
    lo, hi = _opening_box(wall, Lx, Ly, Lz, center_frac, size, eps=0.0)
    center = tuple((a + b) / 2 for a, b in zip(lo, hi))
    x, y, z = _rect_outline(center, wall, size)
    fig.add_trace(go.Scatter3d(
        x=x, y=y, z=z, mode="lines", line=dict(color=color, width=5),
        name=label, customdata=[f"{label}_outline"], showlegend=True,
    ))
    direction = _WALL_FLOW_DIRECTION[wall]
    arrow_len = min(Lx, Ly, Lz) * 0.15
    tip = tuple(c + d * arrow_len for c, d in zip(center, direction))
    fig.add_trace(go.Scatter3d(
        x=[center[0], tip[0]], y=[center[1], tip[1]], z=[center[2], tip[2]],
        mode="lines+markers", line=dict(color=color, width=4),
        marker=dict(size=[0, 5], color=color, symbol="diamond"),
        name=label + " flow", customdata=[f"{label}_arrow"], showlegend=False,
    ))
    return fig


def _add_fan(fig, center, radius, direction, n_points=32):
    cx, cy, cz = center
    dx, dy, dz = direction
    mag = (dx ** 2 + dy ** 2 + dz ** 2) ** 0.5
    dx, dy, dz = dx / mag, dy / mag, dz / mag

    # Build an orthonormal basis (u, v) perpendicular to the fan's own axis,
    # so the disk outline is drawn in the plane normal to `direction`.
    arbitrary = (1, 0, 0) if abs(dz) > 0.9 else (0, 0, 1)
    u = np.cross(direction, arbitrary)
    u = u / np.linalg.norm(u)
    v = np.cross(direction, u)

    theta = np.linspace(0, 2 * np.pi, n_points)
    circle = np.array([cx, cy, cz]) + radius * (np.outer(np.cos(theta), u) + np.outer(np.sin(theta), v))
    fig.add_trace(go.Scatter3d(
        x=circle[:, 0], y=circle[:, 1], z=circle[:, 2], mode="lines",
        line=dict(color="#e8a13a", width=5),
        name="Fan", customdata=["fan_outline"], showlegend=True,
    ))
    arrow_len = radius * 1.2
    tip = (cx + dx * arrow_len, cy + dy * arrow_len, cz + dz * arrow_len)
    fig.add_trace(go.Scatter3d(
        x=[cx, tip[0]], y=[cy, tip[1]], z=[cz, tip[2]],
        mode="lines+markers", line=dict(color="#e8a13a", width=5),
        marker=dict(size=[0, 6], color="#e8a13a", symbol="diamond"),
        name="Fan direction", customdata=["fan_arrow"], showlegend=False,
    ))
    return fig


def plot_case(room, inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
              outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
              fan_center=None, fan_disk_radius=None, fan_direction=(0, 0, -1), fan_speed=None,
              title=""):
    """Build the full preview figure: room + lamps (RoomPlotter) + inlet/
    outlet + optional fan + wall labels. Returns a plotly Figure - render
    with fig.show() or fig.write_html(path).
    """
    fig = RoomPlotter(room).plotly(title=title)
    fig = _add_wall_labels(fig, room.x, room.y, room.z)
    fig = _add_opening(fig, "inlet", inlet_wall, inlet_center, inlet_size,
                        room.x, room.y, room.z, color="#2ecc71")
    fig = _add_opening(fig, "outlet", outlet_wall, outlet_center, outlet_size,
                        room.x, room.y, room.z, color="#e74c3c")
    if fan_speed is not None:
        center = fan_center or (room.x / 2, room.y / 2, room.z - 0.3)
        radius = fan_disk_radius or 0.6
        fig = _add_fan(fig, center, radius, fan_direction)
    return fig
