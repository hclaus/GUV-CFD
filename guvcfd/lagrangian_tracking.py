"""Lagrangian particle tracking through a frozen (converged, steady) CFD
velocity field - computes the true full-trajectory UV dose each fluid
parcel accumulates from inlet to outlet.

Why this exists (see dose_distribution.py and age_analysis.py docstrings
for the fuller story): the Eulerian "age field" snapshot approach used
elsewhere (dose[x] = fluenceRate[x] * age[x]) evaluates LOCAL fluence at
a point times HOW LONG THE PARCEL AT THAT POINT HAS BEEN TRAVELING SO
FAR - it is NOT the total dose that parcel will have accumulated by the
time it actually exits the room, because it hasn't finished its journey
yet. Blatchley's segregated-flow model needs the dose integrated over
each parcel's ENTIRE trajectory (until exit), weighted by how much of
the inlet's volumetric flow that parcel represents - "integration over
the full time, to infinity" in RTD terms. This module gets that directly
by seeding particles at the inlet (flux-weighted) and numerically
integrating their paths through the solved velocity field with RK4,
accumulating dose = integral of fluenceRate along the path, until each
particle reaches the outlet (or a time cap, for particles trapped in a
stagnant recirculation zone).

Advection along the solved (mean, RANS) velocity field alone isn't the
whole story either: a RANS solve only resolves the MEAN flow, and
represents everything the turbulence actually does statistically via
the eddy viscosity field (nut) - real turbulent eddies disperse fluid
that pure mean-flow advection would otherwise trap forever in a
near-zero-mean-velocity recirculation pocket. integrate_particles adds
that back in as a stochastic random-walk term scaled by nut (see its
`diffuse` parameter's docstring) - without it, particles trapped in
slow recirculation get systematically excluded from the dose/RTD
statistics (a real survivorship bias), understating how much of the
room's air genuinely does eventually reach the outlet.
"""
import numpy as np
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator

from .case_io import (
    latest_time_dir, read_cell_centers, read_openfoam_scalar_field, read_openfoam_vector_field,
    read_patch_face_areas, read_patch_face_centers,
)


class FlowFieldInterpolator:
    """Interpolates velocity U and any number of named scalars (e.g.
    fluenceRate, nut) at arbitrary points from their cell-center values,
    all in one combined query (one shared Delaunay tessellation, one
    lookup per point) rather than a separate interpolator per field.

    Uses linear interpolation over a Delaunay tessellation of the cell
    centers (matches what ParaView's own streamline tracer does
    internally); falls back to nearest-cell for points outside the
    tessellation's convex hull (a particle that strays past the
    outermost layer of cell centers toward a wall - normal near a
    no-slip boundary, since cell centers sit half a cell inset from the
    true wall).
    """

    def __init__(self, cell_centers, U, scalars):
        """scalars: dict[str, (N,) array]."""
        cell_centers = np.asarray(cell_centers, dtype=float)
        self._scalar_names = list(scalars.keys())
        columns = [np.asarray(U, dtype=float)] + [np.asarray(scalars[k], dtype=float)[:, None]
                                                    for k in self._scalar_names]
        values = np.column_stack(columns)
        self._linear = LinearNDInterpolator(cell_centers, values)
        self._nearest = NearestNDInterpolator(cell_centers, values)
        self.mins = cell_centers.min(axis=0)
        self.maxs = cell_centers.max(axis=0)

    def __call__(self, points):
        """points: (M,3) array. Returns (U: (M,3), scalars: dict[str, (M,)])."""
        points = np.atleast_2d(np.asarray(points, dtype=float))
        out = self._linear(points)
        bad = np.isnan(out).any(axis=1)
        if bad.any():
            out[bad] = self._nearest(points[bad])
        scalars = {name: out[:, 3 + i] for i, name in enumerate(self._scalar_names)}
        return out[:, :3], scalars

    def clip_to_domain(self, points, margin_frac=1e-3):
        """Clip points to just inside the cell-center bounding box, so a
        particle that overshoots past the outermost cell layer (e.g. a
        large RK4 step near a wall, or a random diffusive kick - see
        integrate_particles) stays within a region the interpolator can
        answer for, instead of drifting arbitrarily far outside where
        nearest-cell fallback becomes a poor approximation.
        """
        margin = margin_frac * (self.maxs - self.mins)
        return np.clip(points, self.mins + margin, self.maxs - margin)


def load_flow_field(case_dir, time_dir=None, include_nut=True):
    """Load cell centers + solved velocity (latest time) + fluenceRate
    (static, time 0) [+ nut, latest time, for turbulent dispersion - see
    integrate_particles' `diffuse` parameter] and build a
    FlowFieldInterpolator.

    Returns (interpolator, cell_centers, time_dir_used).
    """
    time_dir = time_dir or latest_time_dir(case_dir)
    cell_centers = read_cell_centers(case_dir, "0")
    U = read_openfoam_vector_field(f"{case_dir}/{time_dir}/U")
    fluence_path = f"{case_dir}/0/fluenceRate"
    fluence = np.asarray(read_openfoam_scalar_field(fluence_path), dtype=float)

    n = len(cell_centers)
    if not (len(U) == n and len(fluence) == n):
        raise RuntimeError(
            f"Field length mismatch: {n} cell centers, {len(U)} U, {len(fluence)} fluenceRate - "
            "the mesh may have changed between time 0 and the latest time directory."
        )
    scalars = {"fluence": fluence}
    if include_nut:
        nut = np.asarray(read_openfoam_scalar_field(f"{case_dir}/{time_dir}/nut"), dtype=float)
        if len(nut) != n:
            raise RuntimeError(f"Field length mismatch: {n} cell centers, {len(nut)} nut.")
        scalars["nut"] = nut

    interpolator = FlowFieldInterpolator(cell_centers, U, scalars)
    return interpolator, cell_centers, time_dir


def seed_inlet_particles(case_dir, interpolator, n_particles, inlet_patch="inlet", rng=None):
    """Seed n_particles at the inlet patch, one per face pick, weighted by
    that face's own volumetric flow contribution (area * local speed) -
    so a non-uniform inlet (e.g. a radial ceiling diffuser, where speed
    varies face to face) is sampled proportionally to how much actual
    flow each part of it carries, not just its area.

    Returns (n_particles, 3) array of seed positions (at face centers).
    """
    rng = rng if rng is not None else np.random.default_rng()
    centers = read_patch_face_centers(case_dir, inlet_patch)
    areas = read_patch_face_areas(case_dir, inlet_patch)
    U_at_faces, _ = interpolator(centers)
    speeds = np.linalg.norm(U_at_faces, axis=1)
    weights = areas * speeds
    total = weights.sum()
    if total <= 0:
        raise RuntimeError(f"Inlet patch '{inlet_patch}' has zero net outward flow - can't seed particles.")
    probs = weights / total
    face_idx = rng.choice(len(centers), size=n_particles, p=probs)
    return centers[face_idx].copy()


def build_outlet_exit_test(case_dir, all_cell_centers, patch_name="outlet", margin=0.05):
    """Build a vectorized is_exit(positions) -> bool array test for whether
    particles have left through the named outlet patch.

    Self-contained from mesh geometry alone (no dependency on
    run_settings.json's wall-naming scheme): the outlet patch's own face
    centers determine which axis is (nearly) constant across the patch
    (the wall-normal axis), which side of the domain that plane sits on
    (comparing to the overall cell-center bounding box - "outward"), and
    the in-plane rectangle particles must fall within, padded by `margin`
    to allow for face centers being inset from the patch's true edges.
    """
    outlet_centers = read_patch_face_centers(case_dir, patch_name)
    spans = outlet_centers.max(axis=0) - outlet_centers.min(axis=0)
    axis = int(np.argmin(spans))
    plane_value = float(outlet_centers[:, axis].mean())

    domain_min = all_cell_centers[:, axis].min()
    domain_max = all_cell_centers[:, axis].max()
    outward_sign = 1.0 if abs(plane_value - domain_max) < abs(plane_value - domain_min) else -1.0

    other_axes = [a for a in range(3) if a != axis]
    lo = outlet_centers[:, other_axes].min(axis=0) - margin
    hi = outlet_centers[:, other_axes].max(axis=0) + margin

    def is_exit(positions):
        positions = np.atleast_2d(positions)
        crossed = outward_sign * (positions[:, axis] - plane_value) >= 0
        in_plane = np.all((positions[:, other_axes] >= lo) & (positions[:, other_axes] <= hi), axis=1)
        return crossed & in_plane

    return is_exit


def _rk4_batch_step(x, dt, interpolator):
    """One deterministic (advection-only) RK4 step for a batch of particle
    positions x: (M,3), with a per-particle step size dt: (M,) (broadcast
    via dt[:,None]) - each particle advances by its own dt, not a single
    shared value (see characteristic_length's docstring for why a single
    global dt is a real performance problem on a real CFD field). Any
    turbulent-dispersion random walk (see integrate_particles) is added
    by the caller on top of this deterministic result.

    Returns (x_new, dose_rate, start_speed, start_nut): dose_rate is the
    RK4-consistent (4th-order weighted average) fluence sampled along
    the step, in the same units as fluenceRate [uW/cm^2] - the caller
    scales by dt*1e-3 to accumulate dose in mJ/cm^2 (see
    dose_distribution.compute_dose_at_cells). start_speed/start_nut are
    |U| and nut at the step's starting position (from the first RK4
    stage, already computed) - reused by the caller (for next-step dt
    sizing, and this step's diffusive kick) without an extra
    interpolator call. start_nut is None if the interpolator wasn't
    built with a "nut" scalar.
    """
    dt_col = dt[:, None]
    U1, S1 = interpolator(x)
    U2, S2 = interpolator(x + 0.5 * dt_col * U1)
    U3, S3 = interpolator(x + 0.5 * dt_col * U2)
    U4, S4 = interpolator(x + dt_col * U3)
    x_new = x + (dt_col / 6.0) * (U1 + 2 * U2 + 2 * U3 + U4)
    dose_rate = (S1["fluence"] + 2 * S2["fluence"] + 2 * S3["fluence"] + S4["fluence"]) / 6.0
    start_speed = np.linalg.norm(U1, axis=1)
    start_nut = S1.get("nut")
    return x_new, dose_rate, start_speed, start_nut


def characteristic_length(cell_centers):
    """A representative cell size, (domain bounding-box volume / cell
    count)^(1/3) - used to scale the adaptive CFL-like step size below.
    """
    n = len(cell_centers)
    volume = np.prod(cell_centers.max(axis=0) - cell_centers.min(axis=0))
    return (volume / max(n, 1)) ** (1.0 / 3.0)


def integrate_particles(starts, interpolator, is_exit_fn, char_length, t_max,
                         cfl_frac=0.5, dt_min=1e-3, dt_max=2.0, max_steps=2_000_000,
                         diffuse=True, rng=None):
    """Batch RK4-integrate every particle in `starts` simultaneously
    through the frozen velocity field until it exits (is_exit_fn) or
    t_max elapses (marked as not-exited / "trapped").

    Each particle's step size is adaptive (CFL-like: cfl_frac *
    char_length / its own local speed, clipped to [dt_min, dt_max]) -
    NOT one fixed dt shared by every particle. A real room's flow field
    spans a huge speed range (e.g. a slow stagnant bulk at ~0.03 m/s vs.
    a diffuser jet at ~0.5+ m/s) - sizing one global dt to the rare fast
    outlier forces every particle to crawl through the (much more
    common) slow bulk in absurdly many tiny steps. dt_max additionally
    guards against near-zero-speed cells (stagnant corners) driving dt
    toward infinity.

    diffuse: if True (default) and the interpolator carries a "nut"
    scalar, add a stochastic turbulent-dispersion term on top of the
    deterministic RK4 advection - an Euler-Maruyama random walk kick
    sqrt(2*nut*dt)*N(0,1) per axis, the standard Lagrangian-stochastic
    treatment for particle tracking in RANS CFD (where only the MEAN
    velocity is resolved; the turbulent fluctuations that drive real
    dispersion are represented statistically via nut, not present in U
    itself). This matters a lot here: without it, a parcel with near-zero
    mean velocity (a stagnant recirculation pocket) can never leave
    except by literally being advected out by U, which pure advection
    alone can trap indefinitely - understating how much real turbulent
    mixing would eventually carry it toward the outlet. nut is used
    directly as the diffusivity (not nut/Sc_t) to match this case's own
    scalarTransport equations for T/age, which use alphaDt=1 (see
    system/controlDict) - i.e. this uses the SAME diffusivity convention
    the CFD's own solved scalar fields already assume, not an
    independently-chosen constant. When diffuse is active, dt is also
    capped by a parabolic (diffusive) stability-like limit -
    cfl_frac*char_length^2/(2*nut) - alongside the advective one, so a
    high-nut region doesn't get an oversized random kick relative to the
    local cell size.

    Still fully vectorized across all active particles each step (a
    handful of interpolator calls covering every active particle at
    once, each with its own dt) - the interpolator's per-point cost is
    what dominates, and batching amortizes it hugely versus a
    per-particle Python loop.

    Returns dict with:
    - t_exit: (N,) array, elapsed time when each particle exited (or
      t_max for particles that never exited)
    - dose: (N,) array, accumulated dose [mJ/cm^2] at exit (or at t_max)
    - exited: (N,) bool array
    - final_position: (N,3) array
    """
    rng = rng if rng is not None else np.random.default_rng()
    x = np.asarray(starts, dtype=float).copy()
    n = len(x)
    t = np.zeros(n)
    dose = np.zeros(n)
    exited = np.zeros(n, dtype=bool)
    active = np.ones(n, dtype=bool)

    U0, S0 = interpolator(x)
    speed = np.linalg.norm(U0, axis=1)
    diffuse = diffuse and "nut" in S0
    nut = S0["nut"].copy() if diffuse else None

    for _ in range(max_steps):
        idx = np.where(active)[0]
        if len(idx) == 0:
            break
        xi = x[idx]
        dt_i = np.clip(cfl_frac * char_length / np.maximum(speed[idx], 1e-9), dt_min, dt_max)
        if diffuse:
            dt_diffusive = cfl_frac * char_length ** 2 / (2.0 * np.maximum(nut[idx], 1e-12))
            dt_i = np.minimum(dt_i, np.clip(dt_diffusive, dt_min, dt_max))
        # Don't let a step blow past t_max for particles about to hit the cap.
        dt_i = np.minimum(dt_i, t_max - t[idx])

        x_new, dose_rate, start_speed, start_nut = _rk4_batch_step(xi, dt_i, interpolator)

        if diffuse:
            kick_std = np.sqrt(2.0 * np.maximum(start_nut, 0.0) * dt_i)
            x_new = x_new + kick_std[:, None] * rng.standard_normal((len(idx), 3))

        dose[idx] += dose_rate * dt_i * 1e-3  # uW/cm^2 * s * 1e-3 -> mJ/cm^2
        t[idx] += dt_i

        # Check exit against the RAW (unclipped) position - real CFD cell
        # centers are inset from the true boundary patches, so the true
        # outlet plane sits just outside the cell-center convex hull.
        # Clipping first would prevent a particle from ever legitimately
        # reaching it. Only clip afterward, so a particle that's still
        # active stays within safe interpolation bounds for its next step.
        just_exited_local = is_exit_fn(x_new)
        capped_local = (~just_exited_local) & (t[idx] >= t_max - 1e-12)
        still_active_local = ~(just_exited_local | capped_local)

        newly_exited = idx[just_exited_local]
        exited[newly_exited] = True
        active[idx[just_exited_local | capped_local]] = False

        # Exited/capped particles keep their true (unclipped) position;
        # still-active ones get clipped for interpolation safety next step.
        x[idx] = x_new
        active_idx = idx[still_active_local]
        x[active_idx] = interpolator.clip_to_domain(x_new[still_active_local])
        speed[active_idx] = start_speed[still_active_local]
        if diffuse:
            nut[active_idx] = start_nut[still_active_local]

    return {
        "t_exit": t,
        "dose": dose,
        "exited": exited,
        "final_position": x,
    }


def run_lagrangian_tracking(case_dir, n_particles=1000, inlet_patch="inlet", outlet_patch="outlet",
                             t_max=None, cfl_frac=0.5, dt_min=1e-3, dt_max=2.0, diffuse=True, seed=None):
    """End-to-end: load the flow field, seed particles at the inlet
    (flux-weighted), integrate every trajectory to exit (or a time cap)
    with a per-particle adaptive step size and (by default) turbulent
    dispersion (see integrate_particles), return per-particle (t_exit,
    dose, exited).

    t_max: defaults to 40x the nominal room-turnover time implied by mean
    speed and domain extent (20x with diffuse=False) - generous enough
    that only genuinely near-stagnant/recirculating particles get
    capped, without an unbounded run time. Doubled when diffusion is
    active since a particle can now escape a stagnant pocket by random
    walk alone, which - unlike being swept out by the mean flow - can
    take a while.
    """
    rng = np.random.default_rng(seed)
    interpolator, cell_centers, time_dir = load_flow_field(case_dir, include_nut=diffuse)
    U_all, _ = interpolator(cell_centers)
    char_length = characteristic_length(cell_centers)

    if t_max is None:
        domain_extent = float(np.max(interpolator.maxs - interpolator.mins))
        mean_speed = float(np.linalg.norm(U_all, axis=1).mean())
        nominal_crossing_time = domain_extent / max(mean_speed, 1e-9)
        t_max = (40.0 if diffuse else 20.0) * nominal_crossing_time

    starts = seed_inlet_particles(case_dir, interpolator, n_particles, inlet_patch=inlet_patch, rng=rng)
    is_exit_fn = build_outlet_exit_test(case_dir, cell_centers, patch_name=outlet_patch)

    result = integrate_particles(starts, interpolator, is_exit_fn, char_length, t_max,
                                  cfl_frac=cfl_frac, dt_min=dt_min, dt_max=dt_max, diffuse=diffuse, rng=rng)
    result["char_length"] = char_length
    result["t_max"] = t_max
    result["time_dir"] = time_dir
    result["starts"] = starts
    return result
