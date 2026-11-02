import pytest


@pytest.fixture(autouse=True)
def isolated_log_dir(tmp_path, monkeypatch):
    """Keep per-request debug logs written during tests out of the real ./logs directory."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
