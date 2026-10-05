"""``HERMES_BROWSER_NO_REAL_PROFILE`` on the config API: the lock is readable, the locked key reads
as off, and no client can write it on (the Desktop consent prompt / settings toggle, the dashboard
form). Keeps config.yaml from carrying a ``true`` the host does not honor."""
import pytest

LOCK = "HERMES_BROWSER_NO_REAL_PROFILE"


@pytest.fixture
def client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _disk_value():
    """Raw on-disk value; ``save_config`` strips defaults, so "off" is ``None`` or ``False``."""
    from hermes_cli.config import read_raw_config
    return (read_raw_config().get("browser") or {}).get("use_real_profile")


def _disk_off():
    return _disk_value() in (None, False)


class TestUnlocked:
    def test_locks_report_open(self, client, monkeypatch):
        monkeypatch.delenv(LOCK, raising=False)
        body = client.get("/api/config/locks").json()
        assert body["browser.use_real_profile"] == {"locked": False, "reason": None}

    def test_put_may_turn_real_profile_on(self, client, monkeypatch):
        monkeypatch.delenv(LOCK, raising=False)
        resp = client.put("/api/config", json={"config": {"browser": {"use_real_profile": True}}})
        assert resp.status_code == 200
        assert _disk_value() is True


class TestLocked:
    @pytest.mark.parametrize("value", ["1", "true"])
    def test_locks_report_reason(self, client, monkeypatch, value):
        monkeypatch.setenv(LOCK, value)
        body = client.get("/api/config/locks").json()
        assert body["browser.use_real_profile"] == {"locked": True, "reason": LOCK}

    def test_get_config_reads_locked_key_as_off(self, client, monkeypatch):
        from hermes_cli.config import save_config
        monkeypatch.delenv(LOCK, raising=False)
        save_config({"browser": {"use_real_profile": True}})
        assert _disk_value() is True
        monkeypatch.setenv(LOCK, "1")
        assert client.get("/api/config").json()["browser"]["use_real_profile"] is False
        # The raw record is masked the same way: it is what the Desktop echoes back on a save.
        assert client.get("/api/config?include_defaults=false").json()["browser"]["use_real_profile"] is False

    def test_put_on_is_refused_and_disk_unchanged(self, client, monkeypatch):
        from hermes_cli.config import save_config
        monkeypatch.delenv(LOCK, raising=False)
        save_config({"browser": {"use_real_profile": False}, "agent": {"max_turns": 50}})
        monkeypatch.setenv(LOCK, "1")
        resp = client.put("/api/config", json={"config": {"browser": {"use_real_profile": True}}})
        assert resp.status_code == 409
        assert LOCK in resp.json()["detail"]
        assert _disk_off()

    def test_put_of_other_keys_still_saves(self, client, monkeypatch):
        from hermes_cli.config import read_raw_config, save_config
        monkeypatch.setenv(LOCK, "1")
        save_config({"agent": {"max_turns": 50}})
        resp = client.put("/api/config", json={"config": {"agent": {"max_turns": 75},
                                                            "browser": {"use_real_profile": False}}})
        assert resp.status_code == 200
        assert read_raw_config()["agent"]["max_turns"] == 75
        assert _disk_off()

    def test_echoed_record_corrects_stale_true_on_disk(self, client, monkeypatch):
        """A client that saves the GET record back (dashboard form) writes the masked ``false``."""
        from hermes_cli.config import save_config
        monkeypatch.delenv(LOCK, raising=False)
        save_config({"browser": {"use_real_profile": True}})
        monkeypatch.setenv(LOCK, "1")
        record = client.get("/api/config").json()
        resp = client.put("/api/config", json={"config": record})
        assert resp.status_code == 200
        assert _disk_off()
