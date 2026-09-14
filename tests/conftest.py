"""Every test runs against its own work dir: nothing touches the real one."""
import pytest


@pytest.fixture(autouse=True)
def _own_work_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CAHOOTS_WORK_DIR", str(tmp_path_factory.mktemp("work")))
    monkeypatch.setenv("CAHOOTS_GUARDRAIL_OFF", "1")
