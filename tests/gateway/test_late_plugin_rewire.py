"""#87770 — plugins loaded after the gateway started get their platform handlers wired.

Invariants (each red on base):
* ``PluginManager.on_plugin_loaded`` fires after a discovery pass with an activation summary per plugin
  (what is live now vs deferred), through the real discovery path.
* ``rewire_plugin_handlers()`` is idempotent: a factory runs once per native client, however often
  the loaded event fires (a force re-discovery hands back NEW factory objects for the same plugin).
* Telegram: a late plugin handler is hoisted ahead of core's catch-all ``filters.COMMAND`` handler.
* Slack: a late ``register_slack_action_handler`` registers one Bolt listener, never two.
* The runner subscribes at boot and re-wires every live adapter on the loop.
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig, Platform
from gateway.run_plugin_rewire import GatewayPluginRewireMixin
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest, discover_plugins, get_plugin_manager
from plugins.platforms.telegram.adapter import TelegramAdapter


def _write_plugin(home: Path, name: str = "late_cmd") -> None:
    d = home / "plugins" / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(f"name: {name}\nversion: '0.1'\ndescription: t\nprovides_tools: [late_tool]\n")
    (d / "__init__.py").write_text(textwrap.dedent('''
        def register(ctx):
            ctx.register_command("late", lambda raw: "hi", description="late")
            ctx.register_platform_handler("telegram", lambda app, adapter: None)
    '''))
    (home / "config.yaml").write_text(f"plugins:\n  enabled: [{name}]\n")


def test_on_plugin_loaded_fires_with_activation_summary_after_a_late_load():
    home = Path(os.environ["HERMES_HOME"])
    manager = get_plugin_manager()
    discover_plugins()  # boot: plugin not on disk yet
    events: list = []
    unsubscribe = manager.on_plugin_loaded(events.append)
    _write_plugin(home)
    discover_plugins(force=True)  # the mid-run load
    late = [e for e in events[-1] if e["name"] == "late_cmd"]
    assert late == [{"name": "late_cmd", "key": "late_cmd",
                     "activated_now": {"gateway_commands": ["late"], "callbacks": ["telegram"]},
                     "deferred": {"tools": ["late_tool"]}}]
    assert len(events) == 1  # only the sweep that added it fires — and only for the newcomer
    discover_plugins(force=True)  # nothing new: no event (the gateway re-wire is not spammed)
    assert len(events) == 1
    unsubscribe()


def _factory_manager(calls: list, plugin: str = "p") -> PluginManager:
    mgr = PluginManager()
    ctx = PluginContext(manifest=PluginManifest(name=plugin, version="0", description=""), manager=mgr)

    def factory(native, adapter):
        calls.append(native)
    ctx.register_platform_handler("telegram", factory)
    return mgr


def test_rewire_is_idempotent_per_native_client():
    calls: list = []
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t", extra={}))
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=_factory_manager(calls)):
        adapter.rewire_plugin_handlers()  # before connect wired anything: nothing to do
        assert calls == []
        adapter._wire_plugin_handlers("app-1")
        adapter.rewire_plugin_handlers()
        adapter.rewire_plugin_handlers()
        assert calls == ["app-1"]
        # A force re-discovery re-registers a NEW factory object for the same plugin: still once.
        with patch("hermes_cli.plugins.get_plugin_manager", return_value=_factory_manager(calls)):
            adapter.rewire_plugin_handlers()
        assert calls == ["app-1"]
        adapter._wire_plugin_handlers("app-2")  # rebuilt native client: wired again, once
        assert calls == ["app-1", "app-2"]


class _PtbApp:
    """``Application.add_handler`` semantics that matter: append to ``handlers[group]``; PTB dispatches
    the first matching handler per group in that list order (tests/gateway mocks the real SDK)."""

    def __init__(self):
        self.handlers: dict = {}

    def add_handler(self, handler, group: int = 0):
        self.handlers.setdefault(group, []).append(handler)


def test_telegram_late_plugin_handler_precedes_core_catch_all():
    app = _PtbApp()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t", extra={}))
    plugin_handler = object()
    mgr = PluginManager()
    ctx = PluginContext(manifest=PluginManifest(name="p", version="0", description=""), manager=mgr)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=mgr):
        adapter._wire_plugin_handlers(app)  # connect(): plugins first (none yet), then core
        adapter._register_handlers(app)
        core = list(app.handlers[0])  # text catch-all, COMMAND catch-all, ..., CallbackQueryHandler
        ctx.register_platform_handler("telegram", lambda a, _adapter: a.add_handler(plugin_handler))
        adapter.rewire_plugin_handlers()
        adapter.rewire_plugin_handlers()
    group0 = app.handlers[0]
    assert group0 == [plugin_handler] + core  # ahead of every core catch-all, core order untouched
    assert app.handlers[99] and plugin_handler not in app.handlers[99]  # the group-99 observer stays alone


def test_slack_late_action_handler_registers_one_listener():
    from plugins.platforms.slack.adapter import SlackAdapter
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-t", extra={}))
    adapter._app = MagicMock()
    mgr = PluginManager()
    ctx = PluginContext(manifest=PluginManifest(name="p", version="0", description=""), manager=mgr)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=mgr):
        adapter._register_plugin_action_handlers()  # connect
        async def cb(ack, body, action):
            pass
        ctx.register_slack_action_handler("late_btn", cb)
        adapter.rewire_plugin_handlers()
        adapter.rewire_plugin_handlers()
    assert [c.args[0] for c in adapter._app.action.call_args_list] == ["late_btn"]


def test_runner_rewires_live_adapters_on_the_loop_when_plugins_load():
    class Runner(GatewayPluginRewireMixin):
        adapters = {Platform.TELEGRAM: MagicMock()}

    _write_plugin(Path(os.environ["HERMES_HOME"]))  # something must actually load for the event to fire
    mgr = PluginManager()

    async def scenario():
        runner = Runner()
        runner._subscribe_plugin_rewire(mgr)
        runner._subscribe_plugin_rewire(mgr)  # a served-profile rescan re-enters: one listener
        await asyncio.to_thread(mgr.discover_and_load, True)  # fires on another thread
        await asyncio.sleep(0)
        return runner.adapters[Platform.TELEGRAM].rewire_plugin_handlers.call_count

    assert asyncio.run(scenario()) == 1


def _write_discord_plugin(home: Path) -> None:
    directory = home / "plugins" / "native_probe"
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        "name: native_probe\nversion: '0.1'\ndescription: native routing probe\n",
        encoding="utf-8",
    )
    (directory / "__init__.py").write_text(
        textwrap.dedent("""
        import asyncio
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import get_active_profile_name
        from agent.secret_scope import get_secret
        from gateway.run import _profile_runtime_scope

        def register(ctx):
            home = get_hermes_home()
            profile = get_active_profile_name() or "default"

            def factory(native, adapter):
                native.bindings.append((get_hermes_home(), adapter))

                async def launch():
                    native.launches.append((get_hermes_home(), get_secret("NATIVE_PROBE_TOKEN")))

                async def on_message(fields):
                    # Native plugins own callback route guards and deferred profile scopes.
                    source = adapter.build_source(**fields)
                    if source.profile_route_rejected or (source.profile or "default") != profile:
                        return
                    with _profile_runtime_scope(home, hydrate_secrets=False):
                        native.handled.append((get_hermes_home(), source.chat_id,
                                               get_secret("NATIVE_PROBE_TOKEN")))

                native.add_listener(on_message, "on_message")
                native.tasks.append(asyncio.create_task(launch()))

            ctx.register_platform_handler("discord", factory)
    """),
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [native_probe]\n", encoding="utf-8"
    )
    (home / ".env").write_text(
        f"NATIVE_PROBE_TOKEN=test-{home.name}\n", encoding="utf-8"
    )


class _NativeDiscord:
    def __init__(self):
        self.bindings, self.launches, self.tasks, self.handled = [], [], [], []
        self.listeners = []

    def add_listener(self, callback, name):
        assert name == "on_message"
        self.listeners.append(callback)

    async def message(self, chat_id):
        for callback in self.listeners:
            await callback({"chat_id": chat_id, "user_id": "7", "chat_type": "channel"})


def _discord_mux(routes):
    from gateway.config import GatewayConfig
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner
    from plugins.platforms.discord.adapter import DiscordAdapter

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.profile_routes = parse_profile_routes(routes)
    runner._primary_profile_name = "default"
    runner._profile_adapters = {}
    runner.pairing_store = object()
    runner.pairing_stores = {}
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-only"))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    return runner, adapter


@pytest.mark.asyncio
async def test_shared_discord_wires_only_served_route_factories(tmp_path, monkeypatch):
    """Regression for canonicledaddy/canonicle-build#541; real discovery, scopes and routes."""
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from gateway.run import _profile_runtime_scope
    from hermes_cli.profiles import profiles_to_serve
    from hermes_constants import get_hermes_home

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    for name in (
        "reviewer",
        "unmatched",
        "disabled",
        "other_bot",
        "other_platform",
        "parked",
    ):
        _write_discord_plugin(home / "profiles" / name)
    ideate = home / "profiles" / "ideate"
    ideate.mkdir()
    (ideate / "config.yaml").write_text("{}\n", encoding="utf-8")
    (home / "profiles" / "parked" / "gateway.parked").touch()
    routes = [
        {"platform": "discord", "profile": "reviewer", "chat_id": "101"},
        {"platform": "discord", "profile": "reviewer", "chat_id": "102"},
        {"platform": "discord", "profile": "ideate", "chat_id": "202"},
        {"platform": "discord", "profile": "default", "chat_id": "203"},
        {
            "platform": "discord",
            "profile": "disabled",
            "chat_id": "303",
            "enabled": False,
        },
        {
            "platform": "discord",
            "profile": "other_bot",
            "chat_id": "304",
            "bot_profile": "other_bot",
        },
        {"platform": "slack", "profile": "other_platform", "chat_id": "305"},
        {"platform": "discord", "profile": "parked", "chat_id": "404"},
    ]
    token = set_multiplex_context(True)
    try:
        with _profile_runtime_scope(home, hydrate_secrets=False):
            discover_plugins()
            default_manager = get_plugin_manager()
            assert default_manager.get_platform_handler_factories("discord") == []
            runner, adapter = _discord_mux(routes)
            native = _NativeDiscord()
            adapter._wire_plugin_handlers(
                native
            )  # Discord connect's existing native wiring seam.
            assert native.bindings == []

            served = profiles_to_serve(multiplex=True)
            for name, profile_home in served:
                if name != "default":
                    await runner._load_secondary_profile_config(name, profile_home)
                    runner._profile_adapters[
                        name
                    ] = {}  # No additional credential/client.
            reviewer = home / "profiles" / "reviewer"
            with _profile_runtime_scope(reviewer, hydrate_secrets=False):
                assert (
                    len(get_plugin_manager().get_platform_handler_factories("discord"))
                    == 1
                )
            assert get_plugin_manager() is default_manager

            runner._record_served_profiles("default", served)
            assert native.bindings == [(reviewer, adapter)]
            assert runner.adapters == {Platform.DISCORD: adapter}
            assert all(not adapters for adapters in runner._profile_adapters.values())
            runner._record_served_profiles("default", served)
            for profile_home in (home, reviewer, home / "profiles" / "unmatched", home):
                with _profile_runtime_scope(profile_home, hydrate_secrets=False):
                    adapter.rewire_plugin_handlers()
            assert runner._rewire_plugin_handlers("reviewer", reviewer) == 1
            for name in (
                "unmatched",
                "disabled",
                "other_bot",
                "other_platform",
                "parked",
            ):
                assert (
                    runner._rewire_plugin_handlers(name, home / "profiles" / name) == 0
                )
            assert native.bindings == [(reviewer, adapter)]
            await asyncio.gather(*native.tasks)
            assert native.launches == [(reviewer, "test-reviewer")]
            assert get_hermes_home() == home

            for chat in ("101", "202", "203", "999", "303", "304", "305", "404", "102"):
                await native.message(chat)
            assert native.handled == [
                (reviewer, "101", "test-reviewer"),
                (reviewer, "102", "test-reviewer"),
            ]
            assert adapter.build_source("202").profile == "ideate"
            assert adapter.build_source("203").profile == "default"
            assert adapter.build_source("999").profile is None
            assert adapter.build_source("404").profile_route_rejected
    finally:
        reset_multiplex_context(token)


@pytest.mark.asyncio
async def test_shared_discord_preserves_default_factory_and_reconnect(
    tmp_path, monkeypatch
):
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from gateway.run import _profile_runtime_scope
    from hermes_cli.profiles import profiles_to_serve

    home = tmp_path / ".hermes"
    reviewer = home / "profiles" / "reviewer"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for profile_home in (home, reviewer):
        _write_discord_plugin(profile_home)
    token = set_multiplex_context(True)
    try:
        with _profile_runtime_scope(home, hydrate_secrets=False):
            discover_plugins()
            runner, adapter = _discord_mux([
                {"platform": "discord", "profile": "reviewer", "chat_id": "101"},
            ])
            native = _NativeDiscord()
            adapter._wire_plugin_handlers(native)
            await runner._load_secondary_profile_config("reviewer", reviewer)
            runner._profile_adapters["reviewer"] = {}
            runner._record_served_profiles("default", profiles_to_serve(multiplex=True))
            # Same plugin name and qualname in two homes: each owns one registration.
            assert native.bindings == [(home, adapter), (reviewer, adapter)]
            await asyncio.gather(*native.tasks)
            await native.message("999")
            await native.message("101")
            assert native.handled == [
                (home, "999", "test-.hermes"),
                (reviewer, "101", "test-reviewer"),
            ]

            rebuilt = _NativeDiscord()
            adapter._wire_plugin_handlers(rebuilt)
            runner._rewire_plugin_handlers("reviewer", reviewer)
            assert rebuilt.bindings == [(home, adapter), (reviewer, adapter)]
            await asyncio.gather(*rebuilt.tasks)
            assert rebuilt.launches == [
                (home, "test-.hermes"),
                (reviewer, "test-reviewer"),
            ]

            runner.config.multiplex_profiles = False
            standalone = _NativeDiscord()
            adapter._wire_plugin_handlers(standalone)
            assert standalone.bindings == [(home, adapter)]
            await asyncio.gather(*standalone.tasks)
    finally:
        reset_multiplex_context(token)


@pytest.mark.asyncio
async def test_shared_discord_primary_lifecycle_from_named_launch(
    tmp_path, monkeypatch
):
    """#541: primary startup/reconnect must not adopt an unrouted launcher's factories."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from gateway.run import GatewayRunner, load_gateway_config_for_runner
    from hermes_cli.profiles import get_active_profile_name
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord import adapter as discord_adapter

    home = tmp_path / ".hermes"
    reviewer = home / "profiles" / "reviewer"
    launcher = home / "profiles" / "launcher"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launcher))
    for profile_home in (home, reviewer, launcher):
        _write_discord_plugin(profile_home)
    for profile_home in (home, launcher):
        config_path = profile_home / "config.yaml"
        with config_path.open("a", encoding="utf-8") as stream:
            stream.write("gateway:\n  multiplex_profiles: true\n")
    with (home / "config.yaml").open("a", encoding="utf-8") as stream:
        stream.write(
            textwrap.dedent("""
            platforms:
              discord:
                enabled: true
                token: test-only
                slash_commands: false
            profile_routes:
              - platform: discord
                profile: reviewer
                chat_id: '101'
        """)
        )

    clients = []

    class Client(_NativeDiscord):
        def __init__(self, **kwargs):
            super().__init__()
            self.user = "test-bot"
            self.guilds = []
            self.closed = asyncio.Event()
            clients.append(self)

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

    monkeypatch.setattr(discord_adapter.commands, "Bot", Client)
    monkeypatch.setattr(discord_adapter.discord.opus, "is_loaded", lambda: True)

    runner = GatewayRunner.__new__(GatewayRunner)
    # The real boot loader discovers launch/default plugins and restores the launch scope.
    runner.config = load_gateway_config_for_runner()
    platform_config = runner.config.platforms[Platform.DISCORD]
    runner._init_runtime_caches()
    runner.adapters, runner._profile_adapters = {}, {}
    runner._restart_requested = runner._draining = False
    runner._shutdown_event = asyncio.Event()
    runner.session_store = MagicMock()
    runner.pairing_store, runner.pairing_stores = object(), {}
    runner.delivery_router = SimpleNamespace(adapters=runner.adapters)
    runner._busy_text_mode = "queue"
    runner._voice_mode = {}
    runner._update_platform_runtime_status = MagicMock()
    runner._restore_secondary_completion_ledgers = MagicMock()
    runner._schedule_planned_restart_replay = MagicMock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    runner._schedule_resume_pending_sessions = MagicMock()

    token = set_multiplex_context(True)
    try:
        assert get_hermes_home() == launcher
        assert get_active_profile_name() == "launcher"
        assert runner._primary_profile_name == "default"

        aborted, enabled, skipped, pending = await runner._start_prefilter_platforms()
        assert not aborted and enabled == 1 and not skipped
        # Discovery imports its own adapter module; patch the class production actually created.
        # Only transport housekeeping is stubbed; connect/disconnect and handler wiring stay real.
        adapter_class = type(pending[0][2])
        monkeypatch.setattr(
            adapter_class, "_run_post_connect_initialization", AsyncMock()
        )
        monkeypatch.setattr(adapter_class, "_start_liveness_probe", lambda self: None)
        results = await runner._start_connect_pending(pending)
        assert await runner._start_aggregate_connect_results(results, [], []) == 1
        first = runner.adapters[Platform.DISCORD]
        initial_bindings = list(clients[0].bindings)
        assert await runner._start_secondary_profile_adapters() == 0
        assert all(not adapters for adapters in runner._profile_adapters.values())
        await asyncio.gather(*clients[0].tasks)

        await first.disconnect()
        runner.adapters.clear()
        runner._failed_platforms[Platform.DISCORD] = {
            "config": platform_config,
            "attempts": 0,
            "next_retry": 0,
        }
        await runner._reconnect_failed_platform(Platform.DISCORD, now=1)
        rebuilt = runner.adapters[Platform.DISCORD]
        assert rebuilt is not first
        assert not runner._failed_platforms
        assert len(clients) == 2 and clients[0].is_closed()
        await asyncio.gather(*clients[1].tasks)

        # Check both real connects together so the red run exposes startup AND reconnect identity.
        assert [client.bindings for client in clients] == [
            [(home, first), (reviewer, first)],
            [(home, rebuilt), (reviewer, rebuilt)],
        ], [
            [bound_home.name for bound_home, _adapter in client.bindings]
            for client in clients
        ]
        assert initial_bindings == [(home, first)]
        assert all(
            client.launches
            == [
                (home, "test-.hermes"),
                (reviewer, "test-reviewer"),
            ]
            for client in clients
        )
        assert get_hermes_home() == launcher
        assert get_active_profile_name() == "launcher"
    finally:
        for adapter in runner.adapters.values():
            await adapter.disconnect()
        for unsubscribe in (runner._plugin_rewire_unsubscribe or {}).values():
            unsubscribe()
        reset_multiplex_context(token)
