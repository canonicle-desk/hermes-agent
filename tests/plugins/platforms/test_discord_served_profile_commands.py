"""Native slash metadata is shared; command handlers remain profile scoped."""

import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, MagicMock

import pytest

discord = pytest.importorskip("discord")

from agent.secret_scope import reset_multiplex_context, set_multiplex_context
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.profile_routing import parse_profile_routes
from gateway.run import GatewayRunner, MultiplexConfigError, _profile_runtime_scope
from hermes_cli.plugins import discover_plugins, get_plugin_command_handler
from hermes_constants import get_hermes_home
from plugins.platforms.discord import adapter as discord_adapter


class Client:
    """Real command tree, fake Discord transport (no network or real identifiers)."""

    def __init__(self, **kwargs):
        self.tree = discord.app_commands.CommandTree(
            discord.Client(intents=discord.Intents.none())
        )
        self.application_id = 123
        self.user = SimpleNamespace(id=123, name="test-bot")
        self.guilds = []
        self.closed = asyncio.Event()
        self.http = SimpleNamespace(
            get_global_commands=AsyncMock(return_value=[]),
            upsert_global_command=AsyncMock(),
            edit_global_command=AsyncMock(),
            delete_global_command=AsyncMock(),
        )

    def event(self, callback):
        setattr(self, callback.__name__, callback)
        return callback

    async def start(self, token):
        await self.on_ready()
        await self.closed.wait()

    async def close(self):
        self.closed.set()

    def is_closed(self):
        return self.closed.is_set()


def _homes(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    served = home / "profiles" / "ideate"
    plugin = served / "plugins" / "slash_probe"
    plugin.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n")
    (served / "config.yaml").write_text("plugins:\n  enabled: [slash_probe]\n")
    (plugin / "plugin.yaml").write_text(
        "name: slash_probe\nversion: '0.1'\ndescription: test\n"
    )
    (plugin / "__init__.py").write_text(
        textwrap.dedent("""
        from hermes_constants import get_hermes_home
        def register(ctx):
            ctx.register_command("collective", lambda raw: (str(get_hermes_home()), raw),
                                 description="Collect ideas", args_hint="<idea>")
    """)
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home, served


def _runner(multiplex):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    runner.config.platforms = {
        Platform.DISCORD: PlatformConfig(enabled=True, token="test-only")
    }
    runner.config.profile_routes = parse_profile_routes([
        {"platform": "discord", "profile": "ideate", "guild_id": "800"},
    ])
    runner._primary_profile_name = "default"
    runner._init_runtime_caches()
    runner.adapters, runner._profile_adapters = {}, {}
    runner._restart_requested = runner._draining = False
    runner._shutdown_event = asyncio.Event()
    runner.session_store = MagicMock()
    runner.pairing_store, runner.pairing_stores = object(), {}
    runner.delivery_router = SimpleNamespace(adapters=runner.adapters)
    runner._busy_text_mode = "queue"
    runner._voice_mode = {}
    runner._update_platform_runtime_status = Mock()
    runner._restore_secondary_completion_ledgers = Mock()
    runner._schedule_planned_restart_replay = Mock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    runner._schedule_resume_pending_sessions = Mock()

    def create(platform, config):
        adapter = discord_adapter.DiscordAdapter(config)
        adapter.gateway_runner = runner
        return adapter

    runner._create_adapter = create
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplex", [True, False])
async def test_startup_merges_before_one_sync_and_routes_text_to_owner(
    tmp_path, monkeypatch, multiplex
):
    home, served = _homes(tmp_path, monkeypatch)
    monkeypatch.setattr(discord_adapter.commands, "Bot", Client)
    monkeypatch.setattr(discord_adapter.discord.opus, "is_loaded", lambda: True)
    monkeypatch.setattr(
        discord_adapter.DiscordAdapter, "_start_liveness_probe", lambda self: None
    )
    monkeypatch.setattr(
        discord_adapter.DiscordAdapter,
        "_sleep_between_command_sync_mutations",
        AsyncMock(),
    )
    runner = _runner(multiplex)
    reached_hold = asyncio.Event()
    original_wait = discord_adapter.DiscordAdapter._await_served_profile_command_sync

    async def wait(adapter):
        reached_hold.set()
        await original_wait(adapter)

    monkeypatch.setattr(
        discord_adapter.DiscordAdapter, "_await_served_profile_command_sync", wait
    )
    token = set_multiplex_context(multiplex)
    try:
        with _profile_runtime_scope(home, {}):
            discover_plugins()
            aborted, _, skipped, pending = await runner._start_prefilter_platforms()
            assert not aborted
            adapter = pending[0][2]
            release = Mock(wraps=adapter.release_command_sync)
            monkeypatch.setattr(adapter, "release_command_sync", release)
            results = await runner._start_connect_pending(pending)
            assert await runner._start_aggregate_connect_results(results, [], []) == 1
            await asyncio.wait_for(reached_hold.wait(), 2)
            if multiplex:
                adapter._client.http.get_global_commands.assert_not_awaited()
                assert not adapter._post_connect_task.done()
            assert await runner._start_secondary_profiles(1, skipped) == (False, 1)
            await asyncio.wait_for(adapter._post_connect_task, 2)
            adapter._client.http.get_global_commands.assert_awaited_once()
            assert release.call_count == int(multiplex)
            command = adapter._client.tree.get_command("collective")
            assert (command is not None) is multiplex
            assert get_plugin_command_handler("collective") is None
            if not multiplex:
                assert not getattr(adapter, "_profile_plugin_commands", {})
                return
            assert (
                command.parameters[0].name == "idea" and command.parameters[0].required
            )
            sent = [
                call.args[1]["name"]
                for call in adapter._client.http.upsert_global_command.await_args_list
            ]
            assert sent.count("collective") == 1
            assert adapter._profile_plugin_commands["collective"] == (
                "ideate",
                "Collect ideas",
                "<idea>",
            )

            interaction = SimpleNamespace(
                id=444,
                channel_id=101,
                guild_id=800,
                channel=SimpleNamespace(
                    id=101,
                    name="ideas",
                    topic=None,
                    guild=SimpleNamespace(id=800, name="test"),
                ),
                user=SimpleNamespace(id=7, name="tester", display_name="Tester"),
                response=SimpleNamespace(defer=AsyncMock()),
                delete_original_response=AsyncMock(),
            )
            adapter._check_slash_authorization = AsyncMock(return_value=True)
            adapter.handle_message = AsyncMock()
            await command.callback(interaction, idea="new idea")
            event = adapter.handle_message.await_args.args[0]
            assert event.text == "/collective new idea"
            assert event.source.profile == "ideate" and event.source.guild_id == "800"
            # The handler is still selected by the owning scope, never copied to the shared bot.
            for profile_home in (home, served, home):
                with _profile_runtime_scope(profile_home, {}):
                    handler = get_plugin_command_handler("collective")
                    if profile_home == served:
                        assert handler("new idea") == (str(served), "new idea")
                    else:
                        assert handler is None
            assert get_hermes_home() == home

            # Both kinds of reconnect: rebuild on the same adapter, then replace the adapter.
            await adapter.disconnect()
            assert await adapter.connect(is_reconnect=True)
            assert adapter._client.tree.get_command("collective") is not None
            await asyncio.wait_for(adapter._post_connect_task, 2)
            await adapter.disconnect()
            runner.adapters.clear()
            runner._failed_platforms[Platform.DISCORD] = {
                "config": runner.config.platforms[Platform.DISCORD],
                "attempts": 0,
                "next_retry": 0,
            }
            await runner._reconnect_failed_platform(Platform.DISCORD, now=1)
            replacement = runner.adapters[Platform.DISCORD]
            assert replacement is not adapter
            assert replacement._client.tree.get_command("collective") is not None
            await asyncio.wait_for(replacement._post_connect_task, 2)
    finally:
        for adapter in runner.adapters.values():
            await adapter.disconnect()
        for unsubscribe in (runner._plugin_rewire_unsubscribe or {}).values():
            unsubscribe()
        reset_multiplex_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("startup failed"),
        MultiplexConfigError("invalid profile"),
        asyncio.CancelledError(),
    ],
)
async def test_failure_releases_once_and_collisions_and_cap_survive_rebuild(
    tmp_path, monkeypatch, caplog, failure
):
    home, _ = _homes(tmp_path, monkeypatch)
    runner = _runner(True)
    with _profile_runtime_scope(home, {}):
        aborted, _, _, pending = await runner._start_prefilter_platforms()
        assert not aborted
        adapter = pending[0][2]
        adapter._client = Client()
        adapter._register_slash_commands()
        builtin = adapter._client.tree.get_command("help")
        release = Mock(wraps=adapter.release_command_sync)
        monkeypatch.setattr(adapter, "release_command_sync", release)
        runner.adapters[Platform.DISCORD] = adapter
        runner._start_secondary_profile_adapters = AsyncMock(side_effect=failure)
        runner._startup_fail_fatal_config = Mock()

        # A cancellation before secondary startup must also release the held adapter.
        async def startup():
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            return await runner._start_secondary_profiles(1, [])

        runner._start_impl = startup
        runner._start_flush_runtime_status = AsyncMock()
        waiting = asyncio.Event()
        original_wait = adapter._await_served_profile_command_sync

        async def wait():
            waiting.set()
            await original_wait()

        adapter._await_served_profile_command_sync = wait
        summary = dict.fromkeys(
            ("total", "unchanged", "updated", "recreated", "created", "deleted"), 0
        )
        adapter._safe_sync_slash_commands = AsyncMock(return_value=summary)
        sync_task = asyncio.create_task(adapter._run_post_connect_initialization())
        await asyncio.wait_for(waiting.wait(), 2)
        adapter._safe_sync_slash_commands.assert_not_awaited()
        if isinstance(failure, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await runner.start()
        else:
            await runner.start()
        release.assert_called_once()
        await asyncio.wait_for(sync_task, 2)
        adapter._safe_sync_slash_commands.assert_awaited_once()
        await asyncio.wait_for(adapter._await_served_profile_command_sync(), 2)

        result = adapter.merge_profile_plugin_commands(
            "ideate", [("help", "Replacement", ""), ("collective", "Collect", "<idea>")]
        )
        assert result["collision"] == ["help"]
        assert adapter._client.tree.get_command("help") is builtin
        assert "existing slash command" in caplog.text
        entries = [(f"plugin-{i}", "Extra", "") for i in range(110)]
        result = adapter.merge_profile_plugin_commands("ideate", entries)
        assert result["dropped"] and len(adapter._client.tree.get_commands()) <= 100
        adapter._client = Client()
        adapter._register_slash_commands()
        assert adapter._client.tree.get_command("help") is not None
        assert adapter._client.tree.get_command("collective") is not None
        assert len(adapter._client.tree.get_commands()) <= 100
