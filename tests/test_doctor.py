

def test_proxy_restart_under_wsl_uses_the_systemd_unit(monkeypatch):
    # 2026-11-24: in WSL there is no schtasks; the proxy is ruti-wsl-proxy.service
    import subprocess as sp
    from ruti import doctor

    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return sp.CompletedProcess(argv, 0, "enabled\n", "")

    monkeypatch.setattr(doctor, "_wait_for_liveliness", lambda s: True)
    assert "restarted ruti-wsl-proxy" in doctor._fix_proxy_restart_systemd(run)
    assert calls[-1] == ["systemctl", "--user", "restart", "ruti-wsl-proxy.service"]
    monkeypatch.setattr(doctor, "_wait_for_liveliness", lambda s: False)
    import pytest as _p
    with _p.raises(RuntimeError):
        doctor._fix_proxy_restart_systemd(run)
    with _p.raises(RuntimeError, match="no systemd user unit"):
        doctor._fix_proxy_restart_systemd(lambda a, **k: sp.CompletedProcess(a, 1, "", ""))
