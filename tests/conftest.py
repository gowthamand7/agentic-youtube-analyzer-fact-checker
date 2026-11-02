import pytest


@pytest.fixture(autouse=True)
def isolated_log_dir(tmp_path, monkeypatch):
    """Keep per-request debug logs written during tests out of the real ./logs directory."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture(autouse=True)
def isolated_report_dir(tmp_path, monkeypatch):
    """Keep stored reports written during tests out of the real ./reports directory."""
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))


@pytest.fixture(autouse=True)
def isolated_knowledge_file(tmp_path, monkeypatch):
    """Keep the user's real knowledge profile out of tests."""
    monkeypatch.setenv("KNOWLEDGE_FILE", str(tmp_path / "profile" / "knowledge.json"))
    from agent.personalize import clear_cache

    clear_cache()
