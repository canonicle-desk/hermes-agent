"""Suppression follows the active auth store across resolver, seed and aux paths."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

from agent import anthropic_credentials as ac
from agent import auxiliary_client as aux
from agent import secret_scope
from agent.credential_pool import load_pool
from gateway.run import _profile_runtime_scope
from hermes_cli.auth_commands import auth_add_command


def _home(path, *, suppressed):
    path.mkdir()
    (path / "config.yaml").write_text("model:\n  provider: anthropic\n")
    (path / "auth.json").write_text(
        json.dumps({
            "suppressed_sources": {"anthropic": suppressed},
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "owned",
                        "source": "manual:hermes_pkce",
                        "auth_type": "oauth",
                        "access_token": "owned-token",
                        "refresh_token": "owned-refresh",
                        "expires_at_ms": int(time.time() * 1000) + 3600000,
                    }
                ]
            },
        })
    )
    return path


def test_suppression_gates_discovery_in_the_active_profile(tmp_path, monkeypatch):
    a = _home(tmp_path / "a", suppressed=["claude_code"])
    b = _home(tmp_path / "b", suppressed=[])
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setattr(ac.Path, "home", lambda: tmp_path)
    original_store = (a / "auth.json").read_bytes()
    reader = Mock(return_value={"accessToken": "borrowed-token"})
    refresh = Mock(side_effect=AssertionError("spent a suppressed refresh token"))
    monkeypatch.setattr(ac, "read_claude_code_credentials", reader)
    monkeypatch.setattr(ac, "_refresh_oauth_token", refresh)
    secret_scope.set_multiplex_active(True)
    try:
        for home, suppressed in ((a, True), (b, False), (a, True)):
            reader.reset_mock()
            with _profile_runtime_scope(home, {}):
                assert ac.resolve_anthropic_token() == "owned-token"
                sources = [e.source for e in load_pool("anthropic").entries()]
                assert ("claude_code" in sources) is not suppressed
                if suppressed:
                    reader.assert_not_called()
            if suppressed:
                # Cover the env-token preference branch, API-key branch and empty-pool fallback.
                for env in (
                    {"ANTHROPIC_TOKEN": "sk-ant-oat01-env"},
                    {"ANTHROPIC_API_KEY": "api-key"},
                    {},
                ):
                    with _profile_runtime_scope(home, env):
                        (home / "auth.json").write_text(
                            json.dumps({
                                "providers": {},
                                "suppressed_sources": {"anthropic": ["claude_code"]},
                            })
                        )
                        assert ac.resolve_anthropic_token() == next(
                            iter(env.values()), None
                        )
                (home / "auth.json").write_bytes(original_store)
                reader.assert_not_called()
        refresh.assert_not_called()
    finally:
        secret_scope.set_multiplex_active(False)


def test_auth_add_preserves_borrowed_suppression_and_aux_never_refreshes_it(
    tmp_path, monkeypatch
):
    home = _home(
        tmp_path / "home",
        suppressed=["claude_code", "hermes_pkce", "env:ANTHROPIC_TOKEN"],
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(ac.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        ac,
        "run_hermes_oauth_login_pure",
        lambda: {
            "access_token": "own-new",
            "refresh_token": "own-new-refresh",
            "expires_at_ms": int(time.time() * 1000) + 3600000,
        },
    )
    external = Mock(
        return_value={
            "accessToken": "borrowed-token",
            "refreshToken": "borrowed-refresh",
        }
    )
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", external)
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_file", external)
    refresh = Mock(side_effect=AssertionError("spent a suppressed refresh token"))
    monkeypatch.setattr(ac, "_refresh_oauth_token", refresh)
    auth_add_command(
        SimpleNamespace(provider="anthropic", auth_type="oauth", label="own")
    )
    assert json.loads((home / "auth.json").read_text())["suppressed_sources"][
        "anthropic"
    ] == ["claude_code"]
    assert aux._refresh_anthropic_credentials("borrowed-token") is False
    external.assert_not_called()
    refresh.assert_not_called()
