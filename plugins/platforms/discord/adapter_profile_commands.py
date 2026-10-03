"""Served-profile slash metadata on a shared Discord application."""

import asyncio
import inspect
import keyword
import logging
import re

logger = logging.getLogger(__name__)
_PLUGIN_SLASH_PLACEHOLDER = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")
_DISCORD_SLASH_NAME = re.compile(r"[a-z0-9_-]{1,32}")


class DiscordProfileCommandsMixin:
    def hold_command_sync(self) -> None:
        if not getattr(self, "_command_sync_hold", False):
            self._command_sync_release = asyncio.Event()
            self._command_sync_hold = True

    def release_command_sync(self) -> None:
        if getattr(self, "_command_sync_hold", False):
            self._command_sync_hold = False
            self._command_sync_release.set()

    async def _await_served_profile_command_sync(self) -> None:
        if getattr(self, "_command_sync_hold", False):
            await self._command_sync_release.wait()

    @staticmethod
    def _plugin_slash_parameter(args_hint: str):
        hint = args_hint.strip()
        if not hint:
            return None
        match = _PLUGIN_SLASH_PLACEHOLDER.fullmatch(hint)
        if match:
            option = match[1].lower()
            if (
                option not in {"interaction", "self"}
                and not keyword.iskeyword(option)
                and _DISCORD_SLASH_NAME.fullmatch(option)
            ):
                return option, hint[:100], True
        return "args", f"Arguments: {hint}"[:100], False

    def _add_profile_plugin_command(self, tree, profile, name, description, args_hint):
        from plugins.platforms.discord.adapter import (
            _DISCORD_MAX_APP_COMMANDS,
            discord,
        )

        names = {command.name for command in tree.get_commands()}
        if name in names:
            logger.warning(
                "[%s] Skipping /%s from profile %s: existing slash command",
                self.name,
                name,
                profile,
            )
            return "collision"
        # Reserve /skill's slot even when this runs before skill registration on reconnect.
        if len(names - {"skill"}) >= _DISCORD_MAX_APP_COMMANDS - 1:
            logger.warning(
                "[%s] Slash cap skipped /%s from profile %s", self.name, name, profile
            )
            return "dropped"
        parameter = self._plugin_slash_parameter(args_hint)
        args, template = (), f"/{name}"
        if parameter:
            option, option_desc, required = parameter
            args = (
                (
                    option,
                    str,
                    inspect.Parameter.empty if required else "",
                    option_desc,
                    None,
                ),
            )
            template += f" {{{option}}}"
        try:
            command = discord.app_commands.Command(
                name=name,
                description=description,
                callback=self._slash_proxy(
                    name, args, template, None, prefix="auto_slash_"
                ),
            )
            if getattr(self, "_hide_slash_commands", False):
                command.default_permissions = discord.Permissions(0)
            tree.add_command(command)
        except Exception:
            logger.warning(
                "[%s] Could not add /%s from profile %s",
                self.name,
                name,
                profile,
                exc_info=True,
            )
            return "collision"
        return "added"

    def merge_profile_plugin_commands(self, profile: str, entries: list) -> dict:
        """Store metadata only; callbacks dispatch text through normal profile routing."""
        result = {key: [] for key in ("added", "duplicate", "collision", "dropped")}
        if not self._slash_commands:
            return result
        if not hasattr(self, "_profile_plugin_commands"):
            self._profile_plugin_commands = {}
        store = self._profile_plugin_commands
        tree = getattr(self._client, "tree", None)
        for command_name, description, args_hint in entries:
            name = command_name.lower()
            if not _DISCORD_SLASH_NAME.fullmatch(name):
                logger.warning(
                    "[%s] Invalid slash name %r from profile %s",
                    self.name,
                    name,
                    profile,
                )
                result["collision"].append(name)
                continue
            metadata = (description[:100] or f"Run /{name}", args_hint.strip())
            previous = store.get(name)
            if previous is not None:
                # ponytail: first schema wins until restart; live schema changes need
                # reconciliation across all owning profiles before replacing a command.
                outcome = "duplicate" if previous[1:] == metadata else "collision"
                if outcome == "collision":
                    logger.warning(
                        "[%s] Skipping /%s from profile %s: conflicting schema",
                        self.name,
                        name,
                        profile,
                    )
            else:
                outcome = (
                    self._add_profile_plugin_command(tree, profile, name, *metadata)
                    if tree is not None
                    else "added"
                )
                if outcome == "added":
                    store[name] = (profile, *metadata)
            result[outcome].append(name)
        return result

    def _reapply_stored_profile_plugin_commands(self, tree) -> int:
        dropped = 0
        for name, (profile, description, args_hint) in list(
            getattr(self, "_profile_plugin_commands", {}).items()
        ):
            outcome = self._add_profile_plugin_command(
                tree, profile, name, description, args_hint
            )
            dropped += outcome == "dropped"
        return dropped
