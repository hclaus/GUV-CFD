"""CLI: compute fluence rate (and UV inactivation rate) at an OpenFOAM case's
cell centers, write them back as new fields, and (with --Z) bin the
inactivation rate into cellZones + fvOptions so pimpleFoam actually applies
it as a sink term on the transported scalar, given an Illuminate .guv
project supplying room/lamp geometry.

Usage:
    python -m openfoam.cli --guv path/to/room.guv --case path/to/case_dir --Z 2.0 \
        [--time 0] [--field-name fluenceRate] [--k-field-name kUV] \
        [--nbins 25] [--source-field T]

If you run `mapFields` (e.g. to transfer a converged flow field from another
case onto this one) run it BEFORE this script, not after - mapFields
overwrites every field with a matching name in both cases' time directories,
including Cx/Cy/Cz/C and any previously-written fluenceRate/kUV if the
source case happens to have them too (e.g. from testing this script against
it earlier). Re-running this script after mapFields is the fix if that
happens - it's cheap and fully regenerates fluenceRate/kUV/cellZones/
fvOptions from the true (post-mapFields) cell centers.
"""
import argparse
from guv_calcs import Project

from .case_io import read_cell_centers, read_boundary_patch_names, write_scalar_field
from .fluence import compute_fluence_at_points, compute_inactivation_rate
from .cellzones import bin_decay_rates, write_cellzones, write_fvoptions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guv", required=True, help="Path to .guv project file")
    ap.add_argument("--case", required=True, help="OpenFOAM case directory")
    ap.add_argument("--time", default="0", help="Time directory to read/write (default: 0)")
    ap.add_argument("--field-name", default="fluenceRate", help="Fluence rate output field name (default: fluenceRate)")
    ap.add_argument("--Z", type=float, default=None,
                     help="UV susceptibility constant in cm^2/mJ. If given, also writes a k field "
                          "(k = Z * E * 1e-3, 1/s) for use as a per-cell inactivation source.")
    ap.add_argument("--k-field-name", default="kUV",
                     help="Inactivation rate output field name (default: kUV - avoid 'k', "
                          "which collides with the turbulence field)")
    ap.add_argument("--nbins", type=int, default=25,
                     help="Number of log-spaced cellZone bins for the fvOptions sink term (default: 25)")
    ap.add_argument("--source-field", default="T",
                     help="Transported scalar field the UV sink term applies to (default: T)")
    args = ap.parse_args()

    print(f"Loading project {args.guv} ...")
    project = Project.load(args.guv)
    room = next(iter(project.rooms.values()))
    print(f"  Room {room.x}x{room.y}x{room.z} {room.units}, {len(room.lamps)} lamp(s)")

    print(f"Reading cell centers from {args.case}/{args.time}/{{Cx,Cy,Cz}} ...")
    points = read_cell_centers(args.case, args.time)
    print(f"  {len(points)} cells, x range [{points[:,0].min():.3f},{points[:,0].max():.3f}], "
          f"y range [{points[:,1].min():.3f},{points[:,1].max():.3f}], "
          f"z range [{points[:,2].min():.3f},{points[:,2].max():.3f}]")

    print("Computing fluence rate at cell centers...")
    values = compute_fluence_at_points(room, points)
    print(f"  Fluence rate range [{values.min():.4g}, {values.max():.4g}]")

    print("Reading boundary patch names...")
    patch_names = read_boundary_patch_names(args.case)
    print(f"  {len(patch_names)} patches: {patch_names}")

    out_path = write_scalar_field(args.case, args.field_name, values, patch_names, time_dir=args.time)
    print(f"Wrote {out_path}")

    if args.Z is not None:
        k_values = compute_inactivation_rate(values, args.Z)
        print(f"Computing inactivation rate k = Z * E * 1e-3 (Z={args.Z} cm^2/mJ)...")
        print(f"  k range [{k_values.min():.4e}, {k_values.max():.4e}] 1/s")
        k_path = write_scalar_field(args.case, args.k_field_name, k_values, patch_names, time_dir=args.time)
        print(f"Wrote {k_path}")

        print(f"Binning into {args.nbins} log-spaced cellZones...")
        bin_idx, bin_repr = bin_decay_rates(k_values, args.nbins)
        zone_names, cz_path = write_cellzones(args.case, bin_idx, args.nbins)
        print(f"Wrote {cz_path}")
        fv_path = write_fvoptions(args.case, zone_names, bin_repr, field_name=args.source_field)
        print(f"Wrote {fv_path}")
        for b in range(args.nbins + 1):
            n = int((bin_idx == b).sum())
            if n > 0:
                print(f"  zone uvZone{b}: {n} cells, k={bin_repr[b]:.4e} /s")


if __name__ == "__main__":
    main()
