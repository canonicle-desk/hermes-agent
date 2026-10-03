"""Native plugin handler wiring shared by platform adapters."""

import logging
from typing import Any, Optional

logger = logging.getLogger("gateway.platforms.base")


def routed_plugin_handler_homes(adapter):
    """Served route targets whose native factories belong on this receiving bot."""
    runner = getattr(adapter, "gateway_runner", None)
    config = getattr(runner, "config", None)
    if not getattr(config, "multiplex_profiles", False):
        return {}
    homes = getattr(runner, "_served_profile_homes", None) or {}
    owner = getattr(adapter, "_owner_profile", None) or "default"
    platform = getattr(adapter.platform, "value", str(adapter.platform))
    return {
        route.profile: homes[route.profile]
        for route in config.profile_routes
        if route.enabled
        and route.platform == platform
        and (route.bot_profile or "default") == owner
        and route.profile != owner
        and route.profile in homes
    }


class PlatformPluginHandlersMixin:
    # Plugin handler factories wired on the live native client: ``(home, plugin, qualname)`` keys, reset when
    # the native client is rebuilt. ``None`` = ``connect()`` has not wired yet (class defaults so
    # subclasses that skip ``super().__init__`` still re-wire safely).
    _plugin_handler_native: Any = None
    _plugin_handlers_wired: Optional[set] = None
    _plugin_handler_home = None

    def _wire_plugin_handlers(self, native: Any = None) -> None:
        """Invoke plugin-registered native handler factories (``ctx.register_platform_handler``)
        with ``(native, adapter)``; adapters call this from ``connect()`` once the native
        client exists and :meth:`rewire_plugin_handlers` re-runs it for plugins loaded later.
        Idempotent per native client: a factory is keyed by ``(home, plugin, qualname)`` and skipped once
        wired on this ``native`` (a force re-discovery hands back NEW function objects for the same
        plugin, so identity alone would double-register). Each factory is isolated so a bad plugin
        can't block connecting. Native callbacks retain their plugin-owned route guards and scopes."""
        from hermes_constants import get_default_hermes_root, get_hermes_home

        runner = getattr(self, "gateway_runner", None)
        multiplex = getattr(
            getattr(runner, "config", None), "multiplex_profiles", False
        )
        if self._plugin_handler_home is None:
            # Primary startup/reconnect can run in a named launcher's ambient scope.
            # Its shared adapter still belongs to default; secondaries connect in their own scope.
            self._plugin_handler_home = (
                get_default_hermes_root()
                if multiplex and not getattr(self, "_owner_profile", None)
                else get_hermes_home()
            )
        if (
            self._plugin_handler_native is not native
            or self._plugin_handlers_wired is None
        ):
            # A rebuilt native client (transient-init rebuild, reconnect) starts with nothing wired.
            self._plugin_handler_native = native
            self._plugin_handlers_wired = set()
        if not multiplex:
            self._wire_profile_plugin_handlers(native)
            return
        from gateway.run import _profile_runtime_scope

        homes = [self._plugin_handler_home, *routed_plugin_handler_homes(self).values()]
        for home in homes:
            try:
                with _profile_runtime_scope(home, hydrate_secrets=False):
                    self._wire_profile_plugin_handlers(native)
            except Exception:
                logger.error(
                    "[%s] Could not scope plugin handlers to %s",
                    self.name,
                    home,
                    exc_info=True,
                )

    def _wire_profile_plugin_handlers(self, native: Any) -> None:
        from hermes_constants import hermes_home_key

        try:
            from hermes_cli.plugins import get_plugin_manager

            factories = get_plugin_manager().get_platform_handler_factories(
                getattr(self.platform, "value", str(self.platform))
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "[%s] Could not load plugin handler factories: %s", self.name, e
            )
            return
        for factory, plugin_name in factories:
            key = (
                hermes_home_key(),
                plugin_name,
                getattr(factory, "__qualname__", None) or repr(factory),
            )
            if key in self._plugin_handlers_wired:
                continue
            try:
                factory(native, self)
                logger.info(
                    "[%s] Wired native handlers from plugin '%s'",
                    self.name,
                    plugin_name,
                )
            except Exception as exc:
                logger.error(
                    "[%s] Plugin '%s' handler factory raised: %s",
                    self.name,
                    plugin_name,
                    exc,
                    exc_info=True,
                )
            # A raising factory is recorded too: re-wire must not re-raise it on every plugin load.
            self._plugin_handlers_wired.add(key)

    def rewire_plugin_handlers(self) -> None:
        """Register handlers of plugins loaded AFTER ``connect()`` wired the first batch (#87770);
        the gateway runner calls this on every plugin-loaded event. Safe to call repeatedly: only
        factories not yet wired on the live native client run. Before ``connect()`` has wired once
        there is nothing to re-wire — connect will pick everything up. Adapters with extra plugin
        registries (Slack action handlers) extend this."""
        if self._plugin_handlers_wired is None:
            return
        self._wire_plugin_handlers(self._plugin_handler_native)
