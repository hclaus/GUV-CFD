"""App version shown in both the Dash app's and the Qt app's title, derived
from git commit count rather than a manually-maintained version string -
guarantees every commit bumps it, with no risk of someone forgetting to.
"""
import subprocess
from pathlib import Path

# Commit count on this branch at the moment "3.00" was defined - every
# commit since adds 0.01, so the shown version increases automatically.
_VERSION_BASELINE_COMMIT_COUNT = 81


def compute_app_version():
    """"Version X.YY", derived from the total number of commits on the
    current branch (git rev-list --count HEAD). 3.00 was defined at
    _VERSION_BASELINE_COMMIT_COUNT commits; the shown version is
    3.(count - baseline), zero-padded to 2 digits. Falls back to a static
    "3.00" if git isn't available (e.g. a packaged/frozen deployment with
    no .git directory) or the count ever regresses below the baseline
    (e.g. a shallow clone).
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=5, check=True,
        )
        count = int(result.stdout.strip())
        return f"3.{max(count - _VERSION_BASELINE_COMMIT_COUNT, 0):02d}"
    except Exception:
        return "3.00"


APP_VERSION = compute_app_version()
