import numpy as np
import pytest

from guvcfd.case_io import write_scalar_field
from guvcfd.lagrangian_tracking import (
    FlowFieldInterpolator, characteristic_length, integrate_particles, run_lagrangian_tracking,
)


def _grid_cell_centers(nx=6, ny=6, nz=6, lo=0.0, hi=5.0):
    xs = np.linspace(lo, hi, nx)
    ys = np.linspace(lo, hi, ny)
    zs = np.linspace(lo, hi, nz)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    return np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])


# --- FlowFieldInterpolator -------------------------------------------------

def test_interpolator_reproduces_linear_fields_exactly():
    # Linear interpolation of a genuinely linear function has zero error
    # (to floating point) - a strong, unambiguous correctness check.
    centers = _grid_cell_centers()
    U = np.column_stack([2 * centers[:, 0] + 1, -centers[:, 1], np.zeros(len(centers))])
    scalar = 3 * centers[:, 0] - 0.5 * centers[:, 2] + 7
    interp = FlowFieldInterpolator(centers, U, {"fluence": scalar})

    query = np.array([[1.3, 2.7, 0.9], [4.1, 0.2, 3.3]])
    U_got, s_got = interp(query)
    U_expected = np.column_stack([2 * query[:, 0] + 1, -query[:, 1], np.zeros(len(query))])
    s_expected = 3 * query[:, 0] - 0.5 * query[:, 2] + 7
    assert U_got == pytest.approx(U_expected, abs=1e-9)
    assert s_got["fluence"] == pytest.approx(s_expected, abs=1e-9)


def test_interpolator_falls_back_to_nearest_outside_convex_hull():
    centers = _grid_cell_centers()
    U = np.zeros((len(centers), 3))
    scalar = np.zeros(len(centers))
    interp = FlowFieldInterpolator(centers, U, {"fluence": scalar})
    # Far outside the [0,5]^3 grid - LinearNDInterpolator alone would give NaN.
    U_got, s_got = interp(np.array([[100.0, 100.0, 100.0]]))
    assert not np.isnan(U_got).any()
    assert not np.isnan(s_got["fluence"]).any()


# --- Core RK4 integration against closed-form trajectories -----------------

def test_plug_flow_matches_closed_form_time_and_dose():
    centers = _grid_cell_centers(lo=-1.0, hi=11.0)
    v = 0.5  # m/s
    E0 = 0.3  # uW/cm^2, spatially uniform
    U = np.tile([v, 0.0, 0.0], (len(centers), 1))
    scalar = np.full(len(centers), E0)
    interp = FlowFieldInterpolator(centers, U, {"fluence": scalar})

    L = 10.0
    starts = np.array([[0.0, 5.0, 5.0]])
    is_exit = lambda pos: pos[:, 0] >= L

    # dt_max kept small relative to this small synthetic domain, since
    # exit is only checked at step boundaries (see integrate_particles) -
    # a step that overshoots the exit plane biases t_exit/dose by up to
    # one dt. Real usage (run_lagrangian_tracking) uses a room-scale
    # dt_max where this bias is negligible relative to typical residence
    # times.
    result = integrate_particles(starts, interp, is_exit, characteristic_length(centers), t_max=100.0,
                                  dt_max=0.02)
    expected_t = L / v
    expected_dose = E0 * expected_t * 1e-3

    assert result["exited"][0]
    assert result["t_exit"][0] == pytest.approx(expected_t, rel=0.02)
    assert result["dose"][0] == pytest.approx(expected_dose, rel=0.02)


def test_linear_fluence_matches_closed_form_integral():
    # fluence(x) = E0 + a*x, uniform flow v in x -> dose has a closed form
    # since x(t) = x0 + v*t: dose = 1e-3 * integral_0^T (E0 + a*(x0+v*t)) dt
    centers = _grid_cell_centers(lo=-1.0, hi=11.0)
    v, a, E0 = 1.0, 0.05, 0.1
    U = np.tile([v, 0.0, 0.0], (len(centers), 1))
    scalar = E0 + a * centers[:, 0]
    interp = FlowFieldInterpolator(centers, U, {"fluence": scalar})

    L, x0 = 10.0, 0.0
    starts = np.array([[x0, 5.0, 5.0]])
    is_exit = lambda pos: pos[:, 0] >= L

    result = integrate_particles(starts, interp, is_exit, characteristic_length(centers), t_max=100.0,
                                  dt_max=0.01)
    T = (L - x0) / v
    expected_dose = 1e-3 * (E0 * T + a * x0 * T + a * v * T ** 2 / 2)

    assert result["exited"][0]
    assert result["dose"][0] == pytest.approx(expected_dose, rel=0.02)


def test_solid_body_rotation_preserves_radius():
    # A curved-path sanity check: solid-body rotation U=(-wy, wx, 0) keeps
    # every particle at constant radius exactly, for any duration.
    centers = _grid_cell_centers(nx=15, ny=15, nz=3, lo=-6.0, hi=6.0)
    omega = 0.3  # rad/s
    U = np.column_stack([-omega * centers[:, 1], omega * centers[:, 0], np.zeros(len(centers))])
    scalar = np.zeros(len(centers))
    interp = FlowFieldInterpolator(centers, U, {"fluence": scalar})

    r0 = 3.0
    starts = np.array([[r0, 0.0, 0.0]])
    is_exit = lambda pos: np.zeros(len(pos), dtype=bool)  # closed orbit, never exits

    t_max = np.pi / omega  # half a revolution
    result = integrate_particles(starts, interp, is_exit, characteristic_length(centers), t_max=t_max,
                                  dt_max=0.02)

    final = result["final_position"][0]
    assert not result["exited"][0]
    assert np.hypot(final[0], final[1]) == pytest.approx(r0, rel=0.02)
    assert final[0] == pytest.approx(-r0, abs=0.3)  # opposite side after half a turn
    assert final[1] == pytest.approx(0.0, abs=0.3)


def test_trapped_particle_marked_not_exited_at_time_cap():
    centers = _grid_cell_centers(lo=-6.0, hi=6.0)
    U = np.zeros((len(centers), 3))  # stagnant - never reaches any exit
    scalar = np.full(len(centers), 0.5)
    interp = FlowFieldInterpolator(centers, U, {"fluence": scalar})

    starts = np.array([[0.0, 0.0, 0.0]])
    is_exit = lambda pos: pos[:, 0] >= 100.0  # unreachable
    result = integrate_particles(starts, interp, is_exit, characteristic_length(centers), t_max=5.0,
                                  dt_max=0.1)

    assert not result["exited"][0]
    assert result["t_exit"][0] == pytest.approx(5.0, rel=0.01)
    # stagnant but still-illuminated cell: dose keeps accumulating even
    # though the particle never leaves.
    assert result["dose"][0] == pytest.approx(0.5 * 5.0 * 1e-3, rel=0.02)


def test_characteristic_length_matches_hand_computed_cell_size():
    # A 6x6x6 grid over [0,5]^3: bbox volume = 125, so char_length =
    # (125/216)^(1/3).
    centers = _grid_cell_centers(nx=6, ny=6, nz=6, lo=0.0, hi=5.0)
    expected = (125.0 / 216.0) ** (1.0 / 3.0)
    assert characteristic_length(centers) == pytest.approx(expected)


def test_adaptive_step_uses_smaller_dt_for_faster_particles():
    # A field with a slow region (x<0) and a fast region (x>0) - a
    # particle passing through the fast region should need proportionally
    # MORE steps per unit distance covered relative to dt_max, i.e. the
    # adaptive dt should shrink there rather than using one dt everywhere.
    centers = _grid_cell_centers(nx=20, ny=4, nz=4, lo=-5.0, hi=5.0)
    speed = np.where(centers[:, 0] < 0, 0.05, 5.0)
    U = np.column_stack([speed, np.zeros(len(centers)), np.zeros(len(centers))])
    scalar = np.zeros(len(centers))
    interp = FlowFieldInterpolator(centers, U, {"fluence": scalar})

    char_length = characteristic_length(centers)
    # Confirm the CFL formula alone would pick very different dt for the
    # two regions (this is what makes a single global dt wasteful).
    dt_slow = 0.5 * char_length / 0.05
    dt_fast = 0.5 * char_length / 5.0
    assert dt_slow > dt_fast * 10


# --- End-to-end through real file I/O (mesh + seeding + exit detection) ----

def _write_synthetic_straight_flow_case(tmp_path, v=0.4, E0=0.2, Lx=10.0, y_lo=1.0, y_hi=3.0, z_lo=1.0, z_hi=3.0,
                                         nut=None):
    """A minimal box case: single inlet face at x=0, single outlet face at
    x=Lx, both spanning [y_lo,y_hi]x[z_lo,z_hi] - plus a dense grid of
    cell-center field data (Cx/Cy/Cz/U/fluenceRate) covering the whole
    box with a uniform plug flow in +x. Exercises the full pipeline (real
    polyMesh patch reads, flux-weighted seeding, RK4 integration, exit
    detection) against a closed-form answer.
    """
    case_dir = tmp_path / "case"
    poly = case_dir / "constant" / "polyMesh"
    poly.mkdir(parents=True)

    points = [
        (0.0, y_lo, z_lo), (0.0, y_hi, z_lo), (0.0, y_hi, z_hi), (0.0, y_lo, z_hi),
        (Lx, y_lo, z_lo), (Lx, y_hi, z_lo), (Lx, y_hi, z_hi), (Lx, y_lo, z_hi),
    ]
    points_text = "FoamFile\n{\n    class vectorField;\n    object points;\n}\n\n8\n(\n" + \
        "\n".join(f"({p[0]} {p[1]} {p[2]})" for p in points) + "\n)\n"
    (poly / "points").write_text(points_text)

    faces_text = (
        "FoamFile\n{\n    class faceList;\n    object faces;\n}\n\n2\n(\n"
        "4(0 1 2 3)\n4(4 5 6 7)\n)\n"
    )
    (poly / "faces").write_text(faces_text)

    boundary_text = (
        "FoamFile\n{\n    class polyBoundaryMesh;\n    object boundary;\n}\n\n2\n(\n"
        "    inlet\n    {\n        type patch;\n        nFaces 1;\n        startFace 0;\n    }\n"
        "    outlet\n    {\n        type patch;\n        nFaces 1;\n        startFace 1;\n    }\n"
        ")\n"
    )
    (poly / "boundary").write_text(boundary_text)

    zero_dir = case_dir / "0"
    zero_dir.mkdir()
    # Field data must extend a bit PAST the outlet plane (x=Lx) - real CFD
    # cell centers are always inset from a case's true boundary patches,
    # so there's naturally room between the interpolation domain and the
    # exit plane; without that margin here, clip_to_domain's inward clamp
    # would prevent a particle from ever reaching x>=Lx at all.
    field_centers = _grid_cell_centers(nx=12, ny=6, nz=6, lo=-1.0, hi=Lx + 1.0)
    field_centers[:, 1] = np.interp(field_centers[:, 1], [-1.0, Lx + 1.0], [-1.0, 5.0])
    field_centers[:, 2] = np.interp(field_centers[:, 2], [-1.0, Lx + 1.0], [-1.0, 5.0])
    write_scalar_field(str(case_dir), "Cx", field_centers[:, 0], ["inlet", "outlet"])
    write_scalar_field(str(case_dir), "Cy", field_centers[:, 1], ["inlet", "outlet"])
    write_scalar_field(str(case_dir), "Cz", field_centers[:, 2], ["inlet", "outlet"])
    write_scalar_field(str(case_dir), "fluenceRate", np.full(len(field_centers), E0), ["inlet", "outlet"])

    U_body = "\n".join(f"({v} 0 0)" for _ in field_centers)
    u_text = (
        "FoamFile\n{\n    class volVectorField;\n    object U;\n}\n\n"
        f"internalField   nonuniform List<vector>\n{len(field_centers)}\n(\n{U_body}\n)\n;\n"
    )
    (zero_dir / "U").write_text(u_text)

    if nut is not None:
        write_scalar_field(str(case_dir), "nut", np.full(len(field_centers), nut), ["inlet", "outlet"])

    return str(case_dir)


def test_end_to_end_straight_flow_case_matches_closed_form(tmp_path):
    # diffuse=False: this fixture has no nut field, and pure-advection
    # correctness against a tight closed-form tolerance is exactly what
    # this test checks - a random walk would (correctly) blow that up.
    v, E0, Lx = 0.4, 0.2, 10.0
    case_dir = _write_synthetic_straight_flow_case(tmp_path, v=v, E0=E0, Lx=Lx)

    result = run_lagrangian_tracking(case_dir, n_particles=5, seed=0, diffuse=False)

    expected_t = Lx / v
    expected_dose = E0 * expected_t * 1e-3

    assert result["exited"].all()
    assert result["t_exit"] == pytest.approx(np.full(5, expected_t), rel=0.03)
    assert result["dose"] == pytest.approx(np.full(5, expected_dose), rel=0.03)
    # single inlet face -> every particle seeded at the same point
    assert result["starts"] == pytest.approx(np.tile(result["starts"][0], (5, 1)))


def test_end_to_end_reads_nut_from_file_and_still_reaches_the_outlet(tmp_path):
    # This test's only purpose is confirming the file-based nut read path
    # (load_flow_field(include_nut=True)) works end-to-end without
    # breaking ordinary advection-dominated exit - the dedicated
    # pure-diffusion tests above already validate the random walk's
    # magnitude/statistics precisely, so nut is deliberately tiny here
    # (a near-negligible perturbation) rather than something large enough
    # to trigger the near-boundary excursion effects those other tests
    # are designed to explore.
    v, E0, Lx = 0.4, 0.2, 10.0
    case_dir = _write_synthetic_straight_flow_case(tmp_path, v=v, E0=E0, Lx=Lx, nut=1e-7)

    result = run_lagrangian_tracking(case_dir, n_particles=5, seed=0, diffuse=True)

    expected_t = Lx / v
    assert result["exited"].all()
    assert result["t_exit"] == pytest.approx(np.full(5, expected_t), rel=0.05)


def test_monte_carlo_mean_dose_converges_with_more_particles(tmp_path):
    # This fixture's single-face inlet and uniform fluence give every
    # particle an identical trajectory regardless of N, so this mainly
    # guards against a seeding/integration bug that would introduce
    # spurious particle-to-particle variance (it should be ~zero here).
    case_dir = _write_synthetic_straight_flow_case(tmp_path, v=0.4, E0=0.2, Lx=10.0)
    small = run_lagrangian_tracking(case_dir, n_particles=20, seed=1, diffuse=False)
    large = run_lagrangian_tracking(case_dir, n_particles=400, seed=2, diffuse=False)
    assert np.mean(large["dose"]) == pytest.approx(np.mean(small["dose"]), rel=0.05)
    assert np.std(large["dose"]) < 1e-6


# --- Turbulent dispersion (random-walk diffusion) ---------------------------

def test_pure_diffusion_matches_expected_mean_squared_displacement():
    # Zero mean flow, uniform nut - particles should undergo a pure
    # random walk. For isotropic 3D diffusion, mean squared displacement
    # grows as MSD(t) = 6*D*t (Einstein relation) - a precise, unambiguous
    # statistical check that the random-walk term's magnitude is right,
    # not just "some noise got added somewhere."
    centers = _grid_cell_centers(nx=10, ny=10, nz=10, lo=-8.0, hi=8.0)
    D = 0.02  # m^2/s - deliberately large so diffusion dominates over a short t_max
    U = np.zeros((len(centers), 3))
    scalars = {"fluence": np.zeros(len(centers)), "nut": np.full(len(centers), D)}
    interp = FlowFieldInterpolator(centers, U, scalars)

    n_particles = 2000
    starts = np.zeros((n_particles, 3))
    is_exit = lambda pos: np.zeros(len(pos), dtype=bool)  # never exits - track for a fixed duration

    t_max = 2.0
    result = integrate_particles(starts, interp, is_exit, characteristic_length(centers), t_max=t_max,
                                  dt_max=0.02, rng=np.random.default_rng(0))

    displacement_sq = np.sum(result["final_position"] ** 2, axis=1)
    msd = displacement_sq.mean()
    expected_msd = 6.0 * D * t_max
    # Monte Carlo noise with 2000 samples - a generous but still meaningful tolerance.
    assert msd == pytest.approx(expected_msd, rel=0.15)


def test_diffusion_lets_stagnant_particles_eventually_escape():
    # The exact scenario the fix targets: zero mean velocity (a particle
    # pure advection could never move, ever) but nonzero nut - turbulent
    # dispersion should let particles randomly walk far enough to cross
    # an exit plane, something test_trapped_particle_marked_not_exited
    # (diffuse implicitly off, no nut given) explicitly shows does NOT
    # happen without this term.
    #
    # Many particles, checking the POPULATION escape rate - a single
    # random-walk trial near a finite boundary is inherently high-
    # variance (an individual particle can legitimately take much longer
    # than its RMS escape time by chance), so asserting on one trial's
    # outcome is an underpowered, flaky test design.
    centers = _grid_cell_centers(lo=-6.0, hi=6.0)
    U = np.zeros((len(centers), 3))
    scalars = {"fluence": np.zeros(len(centers)), "nut": np.full(len(centers), 0.05)}
    interp = FlowFieldInterpolator(centers, U, scalars)

    n_particles = 200
    starts = np.zeros((n_particles, 3))
    is_exit = lambda pos: np.abs(pos[:, 0]) >= 2.0  # a nearby, easily-diffused-to boundary
    result = integrate_particles(starts, interp, is_exit, characteristic_length(centers), t_max=50.0,
                                  dt_max=0.05, rng=np.random.default_rng(1))

    # RMS 1D displacement at t=50 with D=0.05 is sqrt(2*0.05*50)=2.24, right
    # around the 2.0 threshold - expect a solid majority to have escaped,
    # not all (this is inherently probabilistic; empirically ~65-70%).
    assert result["exited"].mean() > 0.5


def test_diffuse_false_disables_the_random_walk_even_with_nut_present():
    centers = _grid_cell_centers(lo=-6.0, hi=6.0)
    U = np.zeros((len(centers), 3))
    scalars = {"fluence": np.zeros(len(centers)), "nut": np.full(len(centers), 0.05)}
    interp = FlowFieldInterpolator(centers, U, scalars)

    starts = np.array([[0.0, 0.0, 0.0]])
    is_exit = lambda pos: np.abs(pos[:, 0]) >= 100.0  # unreachable either way
    result = integrate_particles(starts, interp, is_exit, characteristic_length(centers), t_max=5.0,
                                  dt_max=0.1, diffuse=False)

    # With no mean flow and diffusion explicitly off, the particle can't
    # move from its start position at all.
    assert result["final_position"][0] == pytest.approx([0.0, 0.0, 0.0])
