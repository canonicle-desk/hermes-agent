"""Compare Discord's raw registry without lossy AppCommand flag conversion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter(desired, remote):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._client = SimpleNamespace(
        application_id=123,
        tree=SimpleNamespace(
            get_commands=lambda: [
                SimpleNamespace(to_dict=lambda tree, p=p: p) for p in desired
            ],
            fetch_commands=AsyncMock(
                side_effect=AssertionError("lossy AppCommand read")
            ),
        ),
        http=SimpleNamespace(
            get_global_commands=AsyncMock(return_value=remote),
            edit_global_command=AsyncMock(),
            delete_global_command=AsyncMock(),
            upsert_global_command=AsyncMock(),
        ),
    )
    adapter._sleep_between_command_sync_mutations = AsyncMock()
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "desired_fields,remote_fields,unchanged",
    [
        ({}, {}, True),
        ({}, {"dm_permission": None}, True),
        ({}, {"integration_types": [0, 1]}, True),
        ({"integration_types": [0]}, {"integration_types": [0, 1]}, False),
        ({"integration_types": []}, {"integration_types": [0, 1]}, False),
        ({"integration_types": [0, 1]}, {"integration_types": [0]}, False),
        ({"contexts": [0, 1]}, {"contexts": [0]}, False),
        (
            {"integration_types": [0, 1], "contexts": [0, 1]},
            {"integration_types": [0, 1], "contexts": [0, 1], "dm_permission": None},
            True,
        ),
    ],
)
async def test_raw_vectors_and_defaults_compare_without_startup_churn(
    desired_fields, remote_fields, unchanged
):
    command = {"name": "collective", "description": "Collect ideas", "type": 1}
    desired = {**command, **desired_fields}
    adapter = _adapter([desired], [{**command, "id": "raw-id", **remote_fields}])
    summary = await adapter._safe_sync_slash_commands()
    http = adapter._client.http
    http.get_global_commands.assert_awaited_once_with(123)
    assert summary["unchanged"] == int(unchanged)
    if unchanged:
        http.upsert_global_command.assert_not_awaited()
    else:
        http.upsert_global_command.assert_awaited_once_with(123, desired)
    http.edit_global_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_ids_are_used_and_non_objects_refuse_before_any_mutation():
    desired = {"name": "help", "description": "New help"}
    remote = [
        {"id": "edit-raw-id", "name": "help", "description": "Old help"},
        {"id": "delete-raw-id", "name": "obsolete"},
    ]
    adapter = _adapter([desired], remote)
    await adapter._safe_sync_slash_commands()
    http = adapter._client.http
    http.edit_global_command.assert_awaited_once_with(123, "edit-raw-id", desired)
    http.delete_global_command.assert_awaited_once_with(123, "delete-raw-id")
    for invalid in (None, [], "invalid", Mock()):
        adapter = _adapter([desired], [*remote, invalid])
        with pytest.raises(RuntimeError, match="JSON object"):
            await adapter._safe_sync_slash_commands()
        adapter._client.http.edit_global_command.assert_not_awaited()
        adapter._client.http.delete_global_command.assert_not_awaited()
        adapter._client.http.upsert_global_command.assert_not_awaited()
