#!/usr/bin/env python3
"""
Interactive tracer analysis: no command-line typing needed!
Just run: python tracer_analyze.py
"""
import sys
from pathlib import Path

def get_case_dir():
    """Ask user for case directory."""
    print("\n" + "="*70)
    print("TRACER ANALYSIS - Age-of-Air & Dose Distribution")
    print("="*70 + "\n")

    while True:
        case_path = input("Enter case directory path (or 'q' to quit): ").strip()
        if case_path.lower() == 'q':
            print("Cancelled.")
            sys.exit(0)

        case = Path(case_path)
        if case.is_dir():
            return case
        print(f"ERROR: Directory not found: {case_path}")
        print("Try again.\n")

def get_params():
    """Ask user for analysis parameters."""
    print("\n" + "-"*70)
    print("CASE PARAMETERS")
    print("-"*70 + "\n")

    # ACH
    while True:
        try:
            ach = float(input("Nominal ACH [1/hr] (e.g., 6): "))
            if ach > 0:
                break
            print("ACH must be positive")
        except ValueError:
            print("Invalid number")

    # Volume
    while True:
        try:
            volume = float(input("Room volume [m³] (e.g., 30): "))
            if volume > 0:
                break
            print("Volume must be positive")
        except ValueError:
            print("Invalid number")

    # Z (sensitivity)
    while True:
        try:
            Z = float(input("Pathogen sensitivity Z [cm²/mJ] (default 6): "))
            if Z > 0:
                break
            print("Z must be positive")
        except ValueError:
            print("Using default Z=6")
            Z = 6.0
            break

    return {"ach": ach, "volume": volume, "Z": Z}

def run_analysis(case_dir, params):
    """Run the tracer demo with given parameters."""
    import subprocess

    cmd = [
        sys.executable,
        "tracer_demo.py",
        str(case_dir),
        "--ach", str(params["ach"]),
        "--volume", str(params["volume"]),
        "--Z", str(params["Z"]),
    ]

    print("\n" + "="*70)
    print("RUNNING ANALYSIS...")
    print("="*70 + "\n")

    result = subprocess.run(cmd, cwd=Path(__file__).parent)
    return result.returncode == 0

def main():
    try:
        case_dir = get_case_dir()
        params = get_params()

        success = run_analysis(case_dir, params)

        if success:
            print("\n" + "="*70)
            print("ANALYSIS COMPLETE!")
            print("="*70 + "\n")
            input("Press Enter to exit...")
        else:
            print("\nAnalysis failed. Check the error messages above.")
            input("Press Enter to exit...")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
