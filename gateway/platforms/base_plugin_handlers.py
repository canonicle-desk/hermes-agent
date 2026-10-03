"""Native plugin handler wiring shared by platform adapters."""

import logging
from typing import Any, Optional

logger = logging.getLogger("gateway.platforms.base")


class PlatformPluginHandlersMixin:
    # Plugin handler factories wired on the live native client: ``(plugin, qualname)`` keys, reset when
    # the native client is rebuilt. ``None`` = ``connect()`` has not wired yet (class defaults so
    # subclasses that skip ``super().__init__`` still re-wire safely).
    _plugin_handler_native: Any = None
    _plugin_handlers_wired: Optional[set] = None

    def _wire_plugin_handlers(self, native: Any = None) -> None:
        """Invoke plugin-registered native handler factories (``ctx.register_platform_handler``)
        with ``(native, adapter)``; adapters call this from ``connect()`` once the native
        client exists and :meth:`rewire_plugin_handlers` re-runs it for plugins loaded later.
        Idempotent per native client: a factory is keyed by ``(plugin, qualname)`` and skipped once
        wired on this ``native`` (a force re-discovery hands back NEW function objects for the same
        plugin, so identity alone would double-register). Each factory is isolated so a bad plugin
        can't block connecting."""
        try:
            from hermes_cli.plugins import get_plugin_manager
            factories = get_plugin_manager().get_platform_handler_factories(
                getattr(self.platform, "value", str(self.platform)))
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[%s] Could not load plugin handler factories: %s", self.name, e)
            return
        if self._plugin_handler_native is not native or self._plugin_handlers_wired is None:
            # A rebuilt native client (transient-init rebuild, reconnect) starts with nothing wired.
            self._plugin_handler_native = native
            self._plugin_handlers_wired = set()
        for factory, plugin_name in factories:
            key = (plugin_name, getattr(factory, "__qualname__", None) or repr(factory))
            if key in self._plugin_handlers_wired:
                continue
            try:
                factory(native, self)
                logger.info("[%s] Wired native handlers from plugin '%s'", self.name, plugin_name)
            except Exception as exc:
                logger.error("[%s] Plugin '%s' handler factory raised: %s", self.name, plugin_name,
                             exc, exc_info=True)
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

