"""Tests for JD Smart authentication retries."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import homeassistant.util.dt as dt_util
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jd_smart.api import (
    JdSmartAuthError,
    JdSmartCannotConnectError,
    JdSmartCredentials,
    JdSmartError,
    JdSmartSnapshot,
    JdSmartTokenRefreshError,
)
from custom_components.jd_smart.const import (
    CONF_COOKIE,
    CONF_TGT,
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_AIR_QUALITY_MONITOR,
    DOMAIN,
    RESYNC_AFTER_SILENT_POLLS,
    UPDATE_AUTH_FAILURE_THRESHOLD,
    auth_refresh_notification_ids,
)
from custom_components.jd_smart.coordinator import (
    JdSmartAuthRetryManager,
    JdSmartCoordinator,
)


def _create_manager(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-id",
        data={CONF_COOKIE: "old-cookie", CONF_TGT: "old-tgt"},
    )
    entry.add_to_hass(hass)
    client = SimpleNamespace(
        credentials=JdSmartCredentials(cookie="old-cookie", tgt="old-tgt"),
        async_refresh_token=AsyncMock(),
    )
    return entry, client, JdSmartAuthRetryManager(hass, entry, client)


def test_backoff_updates_one_notification_and_caps_at_one_hour(hass) -> None:
    """The notification is updated with the changing retry time."""
    _entry, _client, manager = _create_manager(hass)
    cancel = Mock()
    expected_delays = [5, 10, 20, 40, 60, 60]

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
            return_value=cancel,
        ) as track,
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ) as create_notification,
    ):
        for attempt, expected_minutes in enumerate(expected_delays, start=1):
            before = dt_util.utcnow()
            manager.async_schedule_failure(JdSmartAuthError("expired"))
            retry_at = track.call_args.args[2]

            assert timedelta(minutes=expected_minutes) <= retry_at - before
            assert retry_at - before < timedelta(minutes=expected_minutes, seconds=1)
            assert (
                create_notification.call_args.kwargs["notification_id"]
                == (auth_refresh_notification_ids("entry-id")[0])
            )
            assert f"Attempt: {attempt}." in create_notification.call_args.args[1]
            manager._retry_cancel = None


async def test_multiple_devices_share_one_immediate_refresh(hass) -> None:
    """Concurrent device failures reuse credentials refreshed by another device."""
    entry, client, manager = _create_manager(hass)

    async def refresh_token():
        client.credentials.tgt = "new-tgt"
        client.credentials.cookie = "new-cookie"
        return "new-tgt", "new-cookie"

    client.async_refresh_token.side_effect = refresh_token

    assert await manager.async_handle_auth_failure("old-tgt")
    assert await manager.async_handle_auth_failure("old-tgt")
    assert not await manager.async_handle_auth_failure("new-tgt")

    client.async_refresh_token.assert_awaited_once()
    assert entry.data[CONF_TGT] == "new-tgt"
    assert entry.data[CONF_COOKIE] == "new-cookie"


async def test_scheduled_retry_validates_and_clears_failure(hass) -> None:
    """A scheduled refresh validates a snapshot before clearing the failure."""
    _entry, client, manager = _create_manager(hass)
    client.async_refresh_token.return_value = ("new-tgt", "new-cookie")
    coordinator = SimpleNamespace(feed_id="feed-id", async_request_refresh=AsyncMock())
    coordinator.async_request_refresh.side_effect = manager.async_mark_recovered
    manager.register_coordinator(coordinator)
    manager._failure_count = 2

    with patch(
        "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
    ) as dismiss_notification:
        await manager._async_retry()

    coordinator.async_request_refresh.assert_awaited_once()
    dismiss_notification.assert_any_call(
        hass, auth_refresh_notification_ids("entry-id")[0]
    )
    assert manager._failure_count == 0


async def test_scheduled_validation_401_reschedules_backoff(hass) -> None:
    """A 401 during scheduled validation schedules another retry."""
    _entry, client, manager = _create_manager(hass)
    client.credentials.tgt = "new-tgt"
    manager._validating_refresh = True

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
            return_value=Mock(),
        ) as track,
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ) as create_notification,
    ):
        assert not await manager.async_handle_auth_failure(
            "new-tgt", JdSmartAuthError("still expired")
        )

    assert not manager._validating_refresh
    track.assert_called_once()
    create_notification.assert_called_once()


async def test_refresh_failure_schedules_retry_without_repeating(hass) -> None:
    """A failed immediate refresh schedules one retry for all later requests."""
    _entry, client, manager = _create_manager(hass)
    client.async_refresh_token.side_effect = JdSmartTokenRefreshError("rejected")

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
            return_value=Mock(),
        ) as track,
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ) as create_notification,
    ):
        assert not await manager.async_handle_auth_failure("old-tgt")
        assert not await manager.async_handle_auth_failure("old-tgt")

    client.async_refresh_token.assert_awaited_once()
    track.assert_called_once()
    create_notification.assert_called_once()


async def test_shutdown_discards_inflight_refresh_result(hass) -> None:
    """An in-flight refresh cannot overwrite credentials after shutdown."""
    entry, client, manager = _create_manager(hass)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh_token():
        refresh_started.set()
        await release_refresh.wait()
        return "stale-tgt", "stale-cookie"

    client.async_refresh_token.side_effect = refresh_token
    refresh_task = asyncio.create_task(manager.async_handle_auth_failure("old-tgt"))
    await refresh_started.wait()
    manager.async_shutdown()
    release_refresh.set()

    assert not await refresh_task
    assert entry.data[CONF_TGT] == "old-tgt"
    assert entry.data[CONF_COOKIE] == "old-cookie"


def test_failure_cleans_legacy_notifications(hass) -> None:
    """New failures dismiss legacy global and per-device notifications."""
    _entry, _client, manager = _create_manager(hass)
    coordinator = SimpleNamespace(feed_id="feed-id")
    manager.register_coordinator(coordinator)

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
            return_value=Mock(),
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
        ) as dismiss_notification,
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ),
    ):
        manager.async_schedule_failure(JdSmartAuthError("expired"))

    dismissed_ids = [call.args[1] for call in dismiss_notification.call_args_list]
    assert dismissed_ids == [
        "jd_smart_token_refresh_failed",
        "jd_smart_feed-id_token_refresh_failed",
    ]


async def test_snapshot_must_validate_refreshed_credentials(hass) -> None:
    """A refresh followed by another 401 remains in the failed state."""
    entry, client, manager = _create_manager(hass)
    client.async_get_snapshot = AsyncMock(
        side_effect=[JdSmartAuthError("expired"), JdSmartAuthError("still expired")]
    )
    client.async_refresh_token.return_value = ("new-tgt", "new-cookie")
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
            return_value=Mock(),
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ) as create_notification,
    ):
        with pytest.raises(UpdateFailed, match="validation failed"):
            await coordinator._async_update_data()

    assert coordinator.auth_retry_pending
    create_notification.assert_called_once()


async def test_successful_refresh_persists_and_clears_notification(hass) -> None:
    """A validated refresh saves credentials and clears retry state."""
    entry, client, manager = _create_manager(hass)
    snapshot = JdSmartSnapshot("digest", "0", True, {"power": "1"})
    client.async_get_snapshot = AsyncMock(
        side_effect=[JdSmartAuthError("expired"), snapshot]
    )
    client.async_refresh_token.return_value = ("new-tgt", "new-cookie")
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )

    with patch(
        "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
    ) as dismiss_notification:
        result = await coordinator._async_update_data()

    assert result is snapshot
    assert entry.data[CONF_TGT] == "new-tgt"
    dismiss_notification.assert_any_call(
        hass, auth_refresh_notification_ids("entry-id")[0]
    )


def test_shutdown_cancels_pending_retry(hass) -> None:
    """Unloading the config entry cancels its retry timer."""
    _entry, _client, manager = _create_manager(hass)
    cancel = Mock()

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
            return_value=cancel,
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ),
    ):
        manager.async_schedule_failure(JdSmartAuthError("expired"))
        manager.async_shutdown()

    cancel.assert_called_once()


async def test_network_failures_never_request_reauthentication(hass) -> None:
    """Connectivity failures keep polling instead of stopping the coordinator.

    Home Assistant stops scheduling refreshes once a coordinator raises
    ConfigEntryAuthFailed, so a transient DNS outage must not escalate. It must
    not flap the entities to unavailable either: below the threshold the
    coordinator serves the previous data instead of raising.
    """
    entry, client, manager = _create_manager(hass)
    snapshot = JdSmartSnapshot("digest", "0", True, {"power": "1"})
    client.async_get_snapshot = AsyncMock(
        side_effect=[JdSmartCannotConnectError("dns cannot resolve")] * 5 + [snapshot]
    )
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    coordinator.data = snapshot

    with patch(
        "custom_components.jd_smart.coordinator.persistent_notification.async_create"
    ) as create_notification:
        for _attempt in range(5):
            # Nothing raises: the previous data is served and the entities
            # stay available through the outage.
            assert await coordinator._async_update_data() is snapshot

        # A transport failure never counts toward the reauth threshold.
        assert coordinator._consecutive_update_failures == 0
        # Recovery serves fresh data on the next poll.
        assert await coordinator._async_update_data() is snapshot

    create_notification.assert_not_called()


async def test_network_failure_resets_reauthentication_counter(hass) -> None:
    """A connectivity failure does not count toward the reauth threshold."""
    entry, client, manager = _create_manager(hass)
    client.async_get_snapshot = AsyncMock(
        side_effect=[
            JdSmartError("unexpected status"),
            JdSmartError("unexpected status"),
            JdSmartCannotConnectError("dns cannot resolve"),
            JdSmartError("unexpected status"),
            JdSmartError("unexpected status"),
        ]
    )
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    coordinator.data = JdSmartSnapshot("digest", "0", True, {"power": "1"})

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
            return_value=Mock(),
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
        ),
    ):
        for _attempt in range(5):
            # Nothing raises below the threshold; previous data is served.
            await coordinator._async_update_data()

        # Two JdSmartError either side of the transport failure: the
        # connectivity error must have reset the streak.
        assert coordinator._consecutive_update_failures == 2

        client.async_get_snapshot.side_effect = JdSmartError("unexpected status")
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()


async def test_shutdown_cancels_fast_polling_and_scheduled_refresh(hass) -> None:
    """Shutting down also runs the base coordinator shutdown."""
    _entry, client, manager = _create_manager(hass)
    coordinator = JdSmartCoordinator(
        hass,
        _entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    fast_poll_cancel = Mock()

    with patch(
        "custom_components.jd_smart.coordinator.async_track_point_in_utc_time",
        return_value=fast_poll_cancel,
    ):
        coordinator.trigger_fast_polling()

    await coordinator.async_shutdown()

    fast_poll_cancel.assert_called_once()
    assert coordinator._shutdown_requested


async def test_silent_device_does_not_accumulate_auth_failures(hass) -> None:
    """Transient errors split by cursorless polls never require reauth.

    The device API answering successfully proves the credentials still work,
    even when the response carries no usable digest. Such a poll must reset the
    consecutive-failure counter, otherwise unrelated errors spread over hours
    add up and are misreported as an authentication failure.
    """
    entry, client, manager = _create_manager(hass)
    previous = JdSmartSnapshot("digest", "0", True, {"power": "1"})
    # A response without a digest is the only kind the coordinator rejects.
    cursorless = JdSmartSnapshot("", "0", False, {})
    client.async_get_snapshot = AsyncMock(
        side_effect=[
            JdSmartError("transient"),
            cursorless,
            JdSmartError("transient"),
            cursorless,
            JdSmartError("transient"),
            cursorless,
        ]
    )
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    coordinator.data = previous

    with (
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ) as create_notification,
    ):
        for _ in range(3):
            # Below the threshold a transient error keeps the entities
            # available by returning the previous data instead of raising.
            assert await coordinator._async_update_data() is previous
            assert await coordinator._async_update_data() is previous

    assert coordinator._consecutive_update_failures == 0
    create_notification.assert_not_called()


async def test_transient_failure_keeps_entities_available(hass) -> None:
    """A failed poll must not flip entities to unavailable before the threshold.

    ``CoordinatorEntity.available`` mirrors ``last_update_success``, and HA sets
    it to ``False`` on the same poll that raises ``UpdateFailed``. Seen in the
    field: one transient error produced
    ``ERROR Error fetching jd_smart data: Unable to update JD Smart`` and every
    entity went unavailable until the next successful poll. The coordinator must
    instead serve the previous data and only surface a sustained outage.
    """
    entry, client, manager = _create_manager(hass)
    previous = JdSmartSnapshot("digest", "0", True, {"power": "1"})
    client.async_get_snapshot = AsyncMock(side_effect=JdSmartError("unexpected status"))
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    coordinator.data = previous

    with (
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ) as create_notification,
    ):
        for _ in range(UPDATE_AUTH_FAILURE_THRESHOLD - 1):
            # Below the threshold the previous data is served and nothing
            # raises, so every entity stays available.
            assert await coordinator._async_update_data() is previous

        assert coordinator._consecutive_update_failures == (
            UPDATE_AUTH_FAILURE_THRESHOLD - 1
        )
        create_notification.assert_not_called()

        # Only a sustained outage escalates and marks the entities unavailable.
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    assert create_notification.called


async def test_silent_device_warns_once_per_outage(hass) -> None:
    """A cursorless snapshot must not log a warning on every single poll."""
    entry, client, manager = _create_manager(hass)
    previous = JdSmartSnapshot("digest", "0", True, {"power": "1"})
    client.async_get_snapshot = AsyncMock(
        return_value=JdSmartSnapshot("", "0", False, {})
    )
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    coordinator.data = previous

    with (
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
        ),
        patch(
            "custom_components.jd_smart.coordinator.LOGGER.warning"
        ) as warning,
    ):
        for _ in range(5):
            assert await coordinator._async_update_data() is previous

    assert warning.call_count == 1


async def test_auth_error_always_rearms_retry_chain(hass) -> None:
    """A failing poll must never leave the retry chain unarmed.

    Regression: the auth-error branch used to raise ``UpdateFailed`` without
    calling ``async_schedule_failure``. Once the previously armed timer fired
    and the refresh failed again, nothing scheduled the next attempt, so the
    coordinator kept polling with dead credentials until a manual reload.
    """
    entry, client, manager = _create_manager(hass)
    client.async_get_snapshot = AsyncMock(
        side_effect=JdSmartAuthError("登录已过期，请重新登录")
    )
    client.async_refresh_token = AsyncMock(
        side_effect=JdSmartTokenRefreshError("WJLogin unreachable")
    )
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    coordinator.data = JdSmartSnapshot("digest", "0", True, {"power": "1"})

    with (
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
        ),
    ):
        for _ in range(3):
            # Simulate the previous timer having fired, which is exactly the
            # state that used to break the chain.
            manager._retry_cancel = None
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
            assert manager._retry_cancel is not None


async def test_schedule_failure_survives_armed_timer(hass) -> None:
    """Re-arming while a timer exists is a no-op, not a reset."""
    _entry, _client, manager = _create_manager(hass)

    with (
        patch(
            "custom_components.jd_smart.coordinator.async_track_point_in_utc_time"
        ) as track,
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ),
    ):
        manager.async_schedule_failure(JdSmartAuthError("expired"))
        first = track.call_count
        manager.async_schedule_failure(JdSmartAuthError("expired again"))
        assert track.call_count == first

    assert manager._failure_count == 1


async def test_retry_without_coordinators_rearms(hass) -> None:
    """A refresh with nothing to validate against must not kill the chain."""
    entry, client, manager = _create_manager(hass)
    client.async_refresh_token = AsyncMock(return_value=("new-tgt", "new-cookie"))

    with patch(
        "custom_components.jd_smart.coordinator.async_track_point_in_utc_time"
    ) as track:
        assert manager._coordinators == []
        await manager._async_retry()
        assert manager._retry_cancel is not None
        assert track.call_count == 1


async def test_shutdown_does_not_schedule_new_timers(hass) -> None:
    """An unloaded entry must not keep arming retry timers."""
    _entry, _client, manager = _create_manager(hass)
    manager.async_shutdown()

    with patch(
        "custom_components.jd_smart.coordinator.async_track_point_in_utc_time"
    ) as track:
        manager.async_schedule_failure(JdSmartAuthError("expired"))

    assert manager._retry_cancel is None
    track.assert_not_called()


async def test_snapshot_without_digest_requests_full_pull(hass) -> None:
    """A response with no digest must trigger a full pull, not a pinned cursor.

    Regression: the endpoint is incremental and echoes the digest we send. If a
    response carries no digest there is no usable cursor, so resending the old
    one would repeat the identical request forever. After
    RESYNC_AFTER_SILENT_POLLS such polls the coordinator must ask for a full
    snapshot (empty digest) to recover.
    """
    entry, client, manager = _create_manager(hass)
    seen_digests: list[str] = []

    async def snapshot(_feed_id, digest=""):
        seen_digests.append(digest)
        if digest == "":
            return JdSmartSnapshot("d100", "0", True, {"curco2": "490"})
        # No cursor came back: nothing we can advance to.
        return JdSmartSnapshot("", "0", False, {})

    client.async_get_snapshot = AsyncMock(side_effect=snapshot)
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air conditioner",
        DEVICE_TYPE_AIR_CONDITIONER,
        manager,
    )
    coordinator.data = JdSmartSnapshot("d099", "0", True, {"curco2": "503"})

    with (
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_dismiss"
        ),
        patch(
            "custom_components.jd_smart.coordinator.persistent_notification.async_create"
        ),
    ):
        stale = await coordinator._async_update_data()
        coordinator.data = stale
        assert stale.streams["curco2"] == "503"

        for _ in range(RESYNC_AFTER_SILENT_POLLS + 1):
            result = await coordinator._async_update_data()
            coordinator.data = result

    assert seen_digests[:3] == ["d099", "d099", "d099"]
    assert "" in seen_digests, "a full pull must be requested to resync"
    assert coordinator.data.streams["curco2"] == "490"
    assert coordinator.data.digest == "d100"


async def test_incremental_snapshot_advances_digest_every_poll(hass) -> None:
    """Every accepted poll must send the digest received from the last one.

    Regression: JD answers ordinary polls with ``fromDeviceSuccess`` false
    while still returning an advanced digest and the full stream set (observed
    as ``streams=11, from_device_success=False``). Rejecting such responses kept
    every entity frozen at the reboot value and resent the same request, so the
    vendor app showed CO2 490 while HA stayed at 503 with no error logged.
    """
    entry, client, manager = _create_manager(hass)
    seen_digests: list[str] = []

    async def snapshot(_feed_id, digest=""):
        seen_digests.append(digest)
        index = len(seen_digests) - 1
        co2 = ["503", "503", "499", "495", "490"][min(index, 4)]
        return JdSmartSnapshot(
            f"rev{index:03d}", "1", False, {"streams0": "0", "curco2": co2}
        )

    client.async_get_snapshot = AsyncMock(side_effect=snapshot)
    coordinator = JdSmartCoordinator(
        hass,
        entry,
        client,
        "feed-id",
        "Air quality monitor",
        DEVICE_TYPE_AIR_QUALITY_MONITOR,
        manager,
    )

    values: list[str] = []
    for _ in range(5):
        result = await coordinator._async_update_data()
        coordinator.data = result
        values.append(result.streams["curco2"])

    assert values == ["503", "503", "499", "495", "490"]
    assert seen_digests == ["", "rev000", "rev001", "rev002", "rev003"]
