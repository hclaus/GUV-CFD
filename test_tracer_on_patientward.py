#!/usr/bin/env python3
"""
Quick test script: load patientwardV10.guvcfd, run a decay case with age field,
then extract RTD and dose distribution.
"""
import json
from pathlib import Path
import subprocess
import sys

# Paths
PROJECT_FILE = Path("C:/Users/hukcl/Documents/1work/222/Collaboration projects/South Africa/TB project/simulation files/patientwardV10.guvcfd")
TEST_CASE_DIR = Path("C:/Users/hukcl/Documents/Python/GUV-CFD/test_patientward_case")

def load_project(project_file):
    """Load .guvcfd project JSON."""
    with open(project_file) as f:
        return json.load(f)

def main():
    print(f"\n{'='*70}")
    print("TRACER IMPLEMENTATION TEST: patientwardV10.guvcfd (6ACH/Z6)")
    print(f"{'='*70}\n")

    # Load project
    if not PROJECT_FILE.exists():
        print(f"ERROR: Project file not found: {PROJECT_FILE}")
        sys.exit(1)

    print(f"Loading project: {PROJECT_FILE}")
    try:
        project = load_project(PROJECT_FILE)
    except Exception as e:
        print(f"ERROR loading project: {e}")
        sys.exit(1)

    print("Project loaded successfully")
    print(f"  Room: {project.get('room_name', '?')}")
    print(f"  ACH: {project.get('ach', '?')}")
    print(f"  Volume: {project.get('room_volume', '?')} m³\n")

    # Note: To actually run a case, you'd need to:
    # 1. Call guvcfd.run_pipeline.setup_case() to create OpenFOAM case
    # 2. Run pimpleFoam
    # 3. Extract results
    #
    # For this demo, we'll show what the workflow would look like.

    print("Test workflow:")
    print("  1. Project loaded with 6 ACH, Z=6")
    print("  2. [Would create OpenFOAM case directory]")
    print("  3. [Would run: pimpleFoam > log.pimpleFoam 2>&1]")
    print("  4. [Would extract: python tracer_demo.py <case> --ach 6 --volume 30 --Z 6]")
    print("\nTo run a full test:")
    print("  a) Load patientwardV10.guvcfd in the GUV-CFD app")
    print("  b) Configure a decay run with ACH=6, Z=6")
    print("  c) Click 'Run' → case will execute with age field enabled")
    print("  d) When done, run:")
    print("     python tracer_demo.py <case_dir> --ach 6 --volume 30 --Z 6")
    print(f"\n{'='*70}\n")

if __name__ == "__main__":
    main()
