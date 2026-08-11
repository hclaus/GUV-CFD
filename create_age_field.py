#!/usr/bin/env python3
"""
Create 0/age initial field file for an existing case without re-running.
This allows age field extraction from completed simulations.
"""
import sys
from pathlib import Path

def create_age_field(case_dir):
    """Create 0/age initial condition file."""
    case_path = Path(case_dir)
    age_file = case_path / "0" / "age"

    # OpenFOAM age field template
    age_content = """FoamFile
{
    version     2.0;
    format      ascii;
    class       volScalarField;
    location    "0";
    object      age;
}

dimensions      [0 0 1 0 0 0 0];

internalField   uniform 0;

boundaryField
{
    inlet
    {
        type            fixedValue;
        value           uniform 0;
    }
    inlet2
    {
        type            fixedValue;
        value           uniform 0;
    }
    outlet
    {
        type            zeroGradient;
    }
    outlet2
    {
        type            zeroGradient;
    }
    xMinWall
    {
        type            zeroGradient;
    }
    xMaxWall
    {
        type            zeroGradient;
    }
    floor
    {
        type            zeroGradient;
    }
    ceiling
    {
        type            zeroGradient;
    }
    frontWall
    {
        type            zeroGradient;
    }
    backWall
    {
        type            zeroGradient;
    }
}
"""

    try:
        # Create 0/age
        age_file.parent.mkdir(parents=True, exist_ok=True)
        with open(age_file, 'w') as f:
            f.write(age_content)

        print(f"Created: {age_file}")
        return True

    except Exception as e:
        print(f"ERROR: Could not create age field")
        print(f"  {e}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python create_age_field.py <case_dir>")
        print("\nExample:")
        print("  python create_age_field.py /path/to/case")
        sys.exit(1)

    case_dir = sys.argv[1]

    print(f"\nCreating 0/age for: {case_dir}\n")

    success = create_age_field(case_dir)

    if success:
        print("\nAge field created successfully!")
        print("Note: This only works if scalarTransport2 was enabled during the run.")
        print("To extract age data, use: python tracer_analyze_simple.py")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
