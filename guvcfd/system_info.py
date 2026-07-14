"""Host machine info for the report's audit-trail row. Read once at report-
generation time (not per-run) via lightweight WMI queries through
PowerShell - this is a Windows-only local tool, and every run happens on
the same machine that later generates the report, so there's no need to
capture this during the run itself.
"""
import subprocess


def _wmi_query(command):
    """Run a one-line PowerShell/WMI query and return its first non-empty
    output line, or None if it fails/times out - system info is a
    nice-to-have on the report, not something that should ever block
    generating it (e.g. this tool running somewhere without PowerShell, or
    a locked-down WMI provider).
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=5,
        )
        lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        return lines[0] if lines else None
    except Exception:
        return None


def get_system_info():
    """{'cpu': str, 'ram_gb': float or None, 'gpu': str or None} for the
    machine this report was generated on. GPU is reported for reference
    only - this pipeline's OpenFOAM solve is CPU-only (see the OpenFOAM
    Notes help section), nothing here runs on it.
    """
    cpu = _wmi_query("(Get-CimInstance Win32_Processor).Name") or "Unknown"
    ram_bytes = _wmi_query("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory")
    ram_gb = round(int(ram_bytes) / (1024 ** 3), 1) if ram_bytes and ram_bytes.isdigit() else None
    gpu = _wmi_query("(Get-CimInstance Win32_VideoController | Select-Object -First 1).Name")
    return {"cpu": cpu, "ram_gb": ram_gb, "gpu": gpu}
