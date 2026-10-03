"""Optional served-profile command metadata must never block profile startup."""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.secret_scope import reset_multiplex_context, set_multiplex_context
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.run_profile_commands import (
    hold_shared_command_sync,
    merge_served_profile_plugin_commands,
    release_shared_command_sync,
    restore_served_profile_plugin_commands,
)
from hermes_constants import get_hermes_home


@pytest.mark.parametrize("configured", [False, True])
def test_incomplete_runner_skips_command_metadata(monkeypatch, caplog, configured):
    runner = GatewayRunner.__new__(GatewayRunner)
    if configured:
        runner.config = GatewayConfig(multiplex_profiles=True)
    enumerate_commands = Mock(side_effect=AssertionError("no adapters to merge into"))
    monkeypatch.setattr("hermes_cli.commands._iter_plugin_command_entries", enumerate_commands)
    adapter = SimpleNamespace(merge_profile_plugin_commands=Mock())

    hold_shared_command_sync(runner, adapter)
    release_shared_command_sync(runner)
    merge_served_profile_plugin_commands(runner, "reviewer")
    restore_served_profile_plugin_commands(runner, adapter)

    enumerate_commands.assert_not_called()
    adapter.merge_profile_plugin_commands.assert_not_called()
    assert not hasattr(runner, "_served_profile_plugin_commands")
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["enumeration", "merge", "restore"])
async def test_command_failure_does_not_abort_profile_config(
    tmp_path, monkeypatch, caplog, failure
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    homes = {name: launch_home / "profiles" / name for name in ("reviewer", "writer")}
    for home in homes.values():
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: []\ngateway:\n  multiplex_profiles: true\n"
        )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    adapter = SimpleNamespace(merge_profile_plugin_commands=Mock())
    runner.adapters = {Platform.DISCORD: adapter}
    error = RuntimeError("command metadata failed")
    if failure == "enumeration":
        # Fail while consuming the generator, not only when constructing it.
        def broken_entries():
            assert get_hermes_home() in homes.values()
            yield ("probe", "Probe", "")
            raise error

        monkeypatch.setattr("hermes_cli.commands._iter_plugin_command_entries", broken_entries)
    elif failure == "merge":
        adapter.merge_profile_plugin_commands.side_effect = error

    token = set_multiplex_context(True)
    try:
        for profile in ("reviewer", "writer", "reviewer"):
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                config = await runner._load_secondary_profile_config(profile, homes[profile])
                if failure == "restore":
                    replacement = SimpleNamespace(merge_profile_plugin_commands=Mock(side_effect=error))
                    restore_served_profile_plugin_commands(runner, replacement)
                    replacement.merge_profile_plugin_commands.assert_called_once()
            assert config.multiplex_profiles
            assert profile in runner._busy_text_modes_by_profile
            assert get_hermes_home() == launch_home
            warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
            assert len(warnings) == 1
            warned_profile = "reviewer" if failure == "restore" else profile
            assert warned_profile in warnings[0].getMessage()
            assert warnings[0].exc_info[1] is error
    finally:
        for unsubscribe in (runner._plugin_rewire_unsubscribe or {}).values():
            unsubscribe()
        reset_multiplex_context(token)
