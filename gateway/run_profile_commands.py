"""Merge served-profile command metadata before the shared application's first sync."""

import logging

logger = logging.getLogger("gateway.run")


def hold_shared_command_sync(runner, adapter) -> None:
    hold = getattr(adapter, "hold_command_sync", None)
    if not runner._multiplex_on() or not callable(hold):
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
                "Could not release slash sync on %s", adapter.name, exc_info=True
            )


def restore_served_profile_plugin_commands(runner, adapter) -> None:
    merge = getattr(adapter, "merge_profile_plugin_commands", None)
    if not runner._multiplex_on() or not callable(merge):
        return
    for profile, entries in getattr(
        runner, "_served_profile_plugin_commands", {}
    ).items():
        merge(profile, entries)


def merge_served_profile_plugin_commands(runner, profile: str) -> None:
    """Caller owns the profile's runtime scope; never retain its handler objects."""
    if not runner._multiplex_on():
        return
    from hermes_cli.commands import _iter_plugin_command_entries

    entries = list(_iter_plugin_command_entries())
    if not hasattr(runner, "_served_profile_plugin_commands"):
        runner._served_profile_plugin_commands = {}
    runner._served_profile_plugin_commands[profile] = entries
    for adapter in runner.adapters.values():
        merge = getattr(adapter, "merge_profile_plugin_commands", None)
        if callable(merge):
            try:
                merge(profile, entries)
            except Exception:
                logger.warning(
                    "Could not merge profile %s commands into %s",
                    profile,
                    adapter.name,
                    exc_info=True,
                )
