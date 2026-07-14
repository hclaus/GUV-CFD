from guvcfd import system_info


def test_get_system_info_parses_wmi_output(monkeypatch):
    def fake_query(command):
        if "Win32_Processor" in command:
            return "12th Gen Intel(R) Core(TM) i7-1255U"
        if "TotalPhysicalMemory" in command:
            return "16849293312"
        if "Win32_VideoController" in command:
            return "Intel(R) Iris(R) Xe Graphics"
        return None

    monkeypatch.setattr(system_info, "_wmi_query", fake_query)
    info = system_info.get_system_info()
    assert info["cpu"] == "12th Gen Intel(R) Core(TM) i7-1255U"
    assert info["ram_gb"] == 15.7
    assert info["gpu"] == "Intel(R) Iris(R) Xe Graphics"


def test_get_system_info_degrades_gracefully_when_queries_fail(monkeypatch):
    monkeypatch.setattr(system_info, "_wmi_query", lambda command: None)
    info = system_info.get_system_info()
    assert info["cpu"] == "Unknown"
    assert info["ram_gb"] is None
    assert info["gpu"] is None


def test_wmi_query_returns_none_on_subprocess_failure(monkeypatch):
    import subprocess

    def raise_error(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=5)

    monkeypatch.setattr(subprocess, "run", raise_error)
    assert system_info._wmi_query("anything") is None
