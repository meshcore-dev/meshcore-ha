"""Tests for discovered contacts FIFO eviction logic."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


DOMAIN = "meshcore"


async def async_evict_discovered_contacts(coord, max_contacts: int) -> bool:
    """Standalone copy of the eviction logic for testability.

    Mirrors MeshCoreDataUpdateCoordinator.async_evict_discovered_contacts.
    """
    if len(coord._discovered_contacts) <= max_contacts:
        return False

    evict_count = len(coord._discovered_contacts) - max_contacts
    keys_to_evict = list(coord._discovered_contacts.keys())[:evict_count]

    entity_registry = coord._entity_registry

    for public_key in keys_to_evict:
        pubkey_prefix = public_key[:12]
        del coord._discovered_contacts[public_key]
        coord.tracked_diagnostic_binary_contacts.discard(pubkey_prefix)

        for entity in list(entity_registry.entities.values()):
            if entity.platform == DOMAIN and entity.domain == "binary_sensor":
                if entity.unique_id == pubkey_prefix:
                    entity_registry.async_remove(entity.entity_id)
                    break

    await coord._store.async_save(coord._discovered_contacts)

    updated_data = dict(coord.data) if coord.data else {}
    updated_data["contacts"] = coord.get_all_contacts()
    coord.async_set_updated_data(updated_data)

    return True


def _make_coordinator(discovered_contacts=None):
    coord = MagicMock()
    coord._discovered_contacts = dict(discovered_contacts or {})
    coord.tracked_diagnostic_binary_contacts = set()
    coord.data = {}
    coord._store = MagicMock()
    coord._store.async_save = AsyncMock()
    coord._entity_registry = MagicMock()
    coord._entity_registry.entities.values.return_value = []
    coord.get_all_contacts = MagicMock(return_value=[])
    return coord


def _make_contacts(n, prefix="pk"):
    contacts = {}
    for i in range(n):
        key = f"{prefix}_{i:04d}_padding_to_make_it_long"
        contacts[key] = {"public_key": key, "adv_name": f"Node {i}"}
    return contacts


@pytest.mark.asyncio
async def test_eviction_under_limit_is_noop():
    contacts = _make_contacts(5)
    coord = _make_coordinator(contacts)

    result = await async_evict_discovered_contacts(coord, max_contacts=10)

    assert result is False
    assert len(coord._discovered_contacts) == 5
    coord._store.async_save.assert_not_called()


@pytest.mark.asyncio
async def test_eviction_exact_limit_is_noop():
    contacts = _make_contacts(10)
    coord = _make_coordinator(contacts)

    result = await async_evict_discovered_contacts(coord, max_contacts=10)

    assert result is False
    assert len(coord._discovered_contacts) == 10


@pytest.mark.asyncio
async def test_eviction_removes_oldest_first():
    contacts = _make_contacts(5)
    coord = _make_coordinator(contacts)
    all_keys = list(contacts.keys())

    result = await async_evict_discovered_contacts(coord, max_contacts=3)

    assert result is True
    assert len(coord._discovered_contacts) == 3

    remaining_keys = list(coord._discovered_contacts.keys())
    assert remaining_keys == all_keys[2:]
    assert all_keys[0] not in coord._discovered_contacts
    assert all_keys[1] not in coord._discovered_contacts


@pytest.mark.asyncio
async def test_eviction_cleans_tracked_set():
    contacts = _make_contacts(3)
    coord = _make_coordinator(contacts)
    all_keys = list(contacts.keys())
    coord.tracked_diagnostic_binary_contacts = {k[:12] for k in all_keys}

    result = await async_evict_discovered_contacts(coord, max_contacts=1)

    assert result is True
    assert len(coord._discovered_contacts) == 1
    assert all_keys[0][:12] not in coord.tracked_diagnostic_binary_contacts
    assert all_keys[1][:12] not in coord.tracked_diagnostic_binary_contacts
    assert all_keys[2][:12] in coord.tracked_diagnostic_binary_contacts


@pytest.mark.asyncio
async def test_eviction_removes_entity_registry_entries():
    contacts = _make_contacts(3)
    coord = _make_coordinator(contacts)
    evicted_key = list(contacts.keys())[0]
    evicted_prefix = evicted_key[:12]

    mock_entity = MagicMock()
    mock_entity.platform = DOMAIN
    mock_entity.domain = "binary_sensor"
    mock_entity.unique_id = evicted_prefix
    mock_entity.entity_id = f"binary_sensor.meshcore_{evicted_prefix}"

    coord._entity_registry.entities.values.return_value = [mock_entity]

    await async_evict_discovered_contacts(coord, max_contacts=2)

    coord._entity_registry.async_remove.assert_called_with(mock_entity.entity_id)


@pytest.mark.asyncio
async def test_eviction_persists_to_storage():
    contacts = _make_contacts(5)
    coord = _make_coordinator(contacts)

    await async_evict_discovered_contacts(coord, max_contacts=3)

    coord._store.async_save.assert_called_once_with(coord._discovered_contacts)
    assert len(coord._discovered_contacts) == 3


@pytest.mark.asyncio
async def test_eviction_triggers_coordinator_update():
    contacts = _make_contacts(5)
    coord = _make_coordinator(contacts)

    await async_evict_discovered_contacts(coord, max_contacts=3)

    coord.async_set_updated_data.assert_called_once()


def test_readvertisement_refreshes_insertion_order():
    """Re-inserting a contact should move it to the back of the dict."""
    contacts = _make_contacts(5)
    keys = list(contacts.keys())
    refreshed_key = keys[1]

    if refreshed_key in contacts:
        del contacts[refreshed_key]
    contacts[refreshed_key] = {"public_key": refreshed_key, "adv_name": "Refreshed"}

    new_keys = list(contacts.keys())
    assert new_keys[-1] == refreshed_key
    assert len(new_keys) == 5
