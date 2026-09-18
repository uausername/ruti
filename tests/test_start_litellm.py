"""The windowless launcher the RutiLiteLLM task runs."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from ruti import doctor, proc

LAUNCHER = Path(__file__).resolve().parent.parent / "litellm" / "start_litellm.pyw"


@pytest.fixture(scope="module")
def launcher():
    loader = importlib.machinery.SourceFileLoader("start_litellm_under_test", str(LAUNCHER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_env_is_parsed_like_the_powershell_script(launcher, tmp_path):
    env = tmp_path / ".env"
    env.write_text("\ufeff# comment\nA_KEY = sk-123 \nSSL_CERT_FILE=C:\\x\\ca.pem\nnot a pair\n",
                   encoding="utf-8")
    assert launcher.load_env(env) == {"A_KEY": "sk-123", "SSL_CERT_FILE": "C:\\x\\ca.pem"}


def test_it_binds_to_loopback_only(launcher, tmp_path):
    argv = launcher.command(tmp_path)
    assert argv[1:] == ["--config", str(tmp_path / "config.yaml"), "--host", "127.0.0.1"]


def test_a_console_launcher_in_the_task_is_flagged(monkeypatch):
    xml = ("<Task><RunLevel>LeastPrivilege</RunLevel><Exec><Command>powershell.exe</Command>"
           "</Exec></Task>")
    monkeypatch.setattr(proc, "run", lambda argv, **_k: proc.Result(argv, 0, xml, "", 0.0))
    check = doctor._check_proxy_task()
    assert check.status == doctor.WARN and "powershell.exe" in check.message

    xml = xml.replace("powershell.exe", '"C:\\Python312\\pythonw.exe"')
    check = doctor._check_proxy_task()
    assert check.status == doctor.OK
