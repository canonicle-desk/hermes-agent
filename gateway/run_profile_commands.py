"""Best-effort served-profile command metadata; failures must not block startup."""

import logging

logger = logging.getLogger("gateway.run")


def hold_shared_command_sync(runner, adapter) -> None:
    hold = getattr(adapter, "hold_command_sync", None)
    if not getattr(getattr(runner, "config", None), "multiplex_profiles", False) or not callable(hold):
        return
    hold()
    if not hasattr(runner, "_held_command_sync_adapters"):
        runner._held_command_sync_adapters = []
    runner._held_command_sync_adapters.append(adapter)


def release_shared_command_sync(runner) -> None:
    held = getattr(runner, "_held_command_sync_adapters", [])
    runner._held_command_sync_adapters = []
    for adapter in held:
        try:
            adapter.release_command_sync()
        except Exception:
            logger.warning(
                "Could not release slash sync on %s", getattr(adapter, "name", "unknown adapter"), exc_info=True
            )


def restore_served_profile_plugin_commands(runner, adapter) -> None:
    profile = "unknown"
    try:
        merge = getattr(adapter, "merge_profile_plugin_commands", None)
        if not getattr(getattr(runner, "config", None), "multiplex_profiles", False) or not callable(merge):
            return
        for profile, entries in getattr(
            runner, "_served_profile_plugin_commands", {}
        ).items():
            merge(profile, entries)
    except Exception:
        logger.warning("Could not restore profile %s commands", profile, exc_info=True)


def merge_served_profile_plugin_commands(runner, profile: str) -> None:
    """Caller owns the profile's runtime scope; never retain its handler objects."""
    try:
        adapters = getattr(runner, "adapters", None)
        if adapters is None or not getattr(getattr(runner, "config", None), "multiplex_profiles", False):
            return
        from hermes_cli.commands import _iter_plugin_command_entries

        entries = list(_iter_plugin_command_entries())
        if not hasattr(runner, "_served_profile_plugin_commands"):
            runner._served_profile_plugin_commands = {}
        runner._served_profile_plugin_commands[profile] = entries
        for adapter in adapters.values():
            merge = getattr(adapter, "merge_profile_plugin_commands", None)
            if callable(merge):
                merge(profile, entries)
    except Exception:
        logger.warning("Could not merge profile %s commands", profile, exc_info=True)
