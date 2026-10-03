"""Raw global-command reconciliation for the Discord adapter."""

from typing import Any, Dict, Optional


class DiscordCommandSyncMixin:
    def _canonicalize_app_command_payload(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Reduce command payloads to the semantic fields Hermes manages."""
        contexts = payload.get("contexts")
        integration_types = payload.get("integration_types")
        return {
            "type": int(payload.get("type", 1) or 1),
            "name": str(payload.get("name", "") or ""),
            "description": str(payload.get("description", "") or ""),
            "default_member_permissions": self._normalize_permissions(
                payload.get("default_member_permissions")
            ),
            "dm_permission": payload.get("dm_permission") is not False,
            "nsfw": bool(payload.get("nsfw", False)),
            "contexts": sorted(int(c) for c in contexts) if contexts else None,
            "integration_types": (
                sorted(int(i) for i in integration_types)
                if integration_types is not None
                else None
            ),
            "options": [
                self._canonicalize_app_command_option(item)
                for item in payload.get("options", []) or []
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _normalize_permissions(value: Any) -> Optional[str]:
        """Normalize default_member_permissions to str-or-None (Discord returns str, discord.py sets int)."""
        if value is None:
            return None
        return str(value)

    def _canonicalize_app_command_option(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            "type": int(payload.get("type", 0) or 0),
            "name": str(payload.get("name", "") or ""),
            "description": str(payload.get("description", "") or ""),
            "required": bool(payload.get("required", False)),
            "autocomplete": bool(payload.get("autocomplete", False)),
            "choices": [
                {
                    "name": str(choice.get("name", "") or ""),
                    "value": choice.get("value"),
                }
                for choice in payload.get("choices", []) or []
                if isinstance(choice, dict)
            ],
            "channel_types": list(payload.get("channel_types", []) or []),
            "min_value": payload.get("min_value"),
            "max_value": payload.get("max_value"),
            "min_length": payload.get("min_length"),
            "max_length": payload.get("max_length"),
            "options": [
                self._canonicalize_app_command_option(item)
                for item in payload.get("options", []) or []
                if isinstance(item, dict)
            ],
        }

    def _patchable_app_command_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Fields supported by discord.py's edit_global_command route."""
        canonical = self._canonicalize_app_command_payload(payload)
        return {
            "name": canonical["name"],
            "description": canonical["description"],
            "options": canonical["options"],
        }

    @staticmethod
    def _canonical_commands_match(
        remote: Dict[str, Any], desired: Dict[str, Any]
    ) -> bool:
        if desired.get("integration_types") is None and remote.get(
            "integration_types"
        ) == [0, 1]:
            remote = {**remote, "integration_types": None}
        return remote == desired

    async def _safe_sync_slash_commands(self) -> Dict[str, int]:
        """Diff existing global commands and only mutate the commands that changed."""
        summary = {
            "total": 0,
            "unchanged": 0,
            "updated": 0,
            "recreated": 0,
            "created": 0,
            "deleted": 0,
        }
        if not self._client:
            return summary
        tree = self._client.tree
        app_id = getattr(self._client, "application_id", None) or getattr(
            getattr(self._client, "user", None), "id", None
        )
        if not app_id:
            raise RuntimeError(
                "Discord application ID is unavailable for slash command sync"
            )
        desired_payloads = [command.to_dict(tree) for command in tree.get_commands()]
        desired_by_key = {
            (
                int(payload.get("type", 1) or 1),
                str(payload.get("name", "") or "").lower(),
            ): payload
            for payload in desired_payloads
        }
        # discord.py 2.7.1 ArrayFlags._from_value right-shifts install/context
        # vectors; AppCommand.to_dict loses them and makes every startup diff.
        http = self._client.http
        raw_commands = await http.get_global_commands(app_id)
        existing_by_key = {}
        for command in raw_commands:
            if not isinstance(command, dict):
                raise RuntimeError(
                    "Discord global command payload was not a JSON object"
                )
            key = (
                int(command.get("type", 1) or 1),
                str(command.get("name", "") or "").lower(),
            )
            existing_by_key[key] = command
        mutation_count = 0

        async def mutate(call, *args):
            nonlocal mutation_count
            if mutation_count:
                await self._sleep_between_command_sync_mutations()
            result = await call(*args)
            mutation_count += 1
            return result

        # Delete obsolete commands FIRST: an upsert pushing the live total over 100 fails with
        # 30032 (breaks ALL slash commands), so an app at the cap must shrink before creating.
        obsolete_keys = set(existing_by_key.keys()) - set(desired_by_key.keys())
        for key in obsolete_keys:
            current = existing_by_key.pop(key)
            await mutate(http.delete_global_command, app_id, current["id"])
            summary["deleted"] += 1
        for key, desired in desired_by_key.items():
            current = existing_by_key.pop(key, None)
            if current is None:
                await mutate(http.upsert_global_command, app_id, desired)
                summary["created"] += 1
                continue
            current_payload = self._canonicalize_app_command_payload(current)
            desired_payload = self._canonicalize_app_command_payload(desired)
            if self._canonical_commands_match(current_payload, desired_payload):
                summary["unchanged"] += 1
                continue
            if self._patchable_app_command_payload(
                current
            ) == self._patchable_app_command_payload(desired):
                # Upsert alone recreates the command: Discord's create endpoint
                # overwrites the existing same-name command ("Returns 201 if a
                # command with the same name does not already exist, or a 200
                # if it does"). Delete-first strands the command deleted when
                # the small command-management bucket 429s the upsert mid-sync;
                # upsert-first keeps the command available even then. The
                # obsolete-path delete-first below is different: an upsert
                # pushing the live total over 100 fails with 30032 (breaks ALL
                # slash commands), so an app at the cap must shrink first.
                await mutate(http.upsert_global_command, app_id, desired)
                summary["recreated"] += 1
                continue
            await mutate(http.edit_global_command, app_id, current["id"], desired)
            summary["updated"] += 1
        summary["total"] = len(desired_payloads)
        return summary
