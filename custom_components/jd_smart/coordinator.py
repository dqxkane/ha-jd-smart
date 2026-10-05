"""Coordinator for the JD Smart integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    JdSmartAuthError,
    JdSmartCannotConnectError,
    JdSmartClient,
    JdSmartError,
    JdSmartSnapshot,
    JdSmartTokenRefreshError,
)
from .const import (
    AUTH_REFRESH_RETRY_DELAYS,
    CONF_COOKIE,
    CONF_SCAN_INTERVAL,
    CONF_TGT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FAST_POLL_DURATION,
    FAST_POLL_INTERVAL,
    LOGGER,
    RESYNC_AFTER_SILENT_POLLS,
    UPDATE_AUTH_FAILURE_THRESHOLD,
    auth_refresh_notification_ids,
)

type JdSmartConfigEntry = ConfigEntry[JdSmartRuntimeData]


def _scan_interval_seconds(data: dict[str, Any]) -> int:
    """Return the configured polling interval in seconds."""
    default = int(DEFAULT_SCAN_INTERVAL.total_seconds())
    try:
        return max(1, int(data.get(CONF_SCAN_INTERVAL, default)))
    except (TypeError, ValueError):
        LOGGER.warning(
            "JD Smart ignoring invalid scan interval %r; using %s seconds",
            data.get(CONF_SCAN_INTERVAL),
            default,
        )
        return default


@dataclass
class JdSmartRuntimeData:
    """Runtime data for JD Smart."""

    client: JdSmartClient
    coordinators: dict[str, JdSmartCoordinator]
    auth_retry_manager: JdSmartAuthRetryManager


class JdSmartAuthRetryManager:
    """Coordinate authentication refresh retries for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: JdSmartConfigEntry,
        client: JdSmartClient,
    ) -> None:
        """Initialize the authentication retry manager."""
        self.hass = hass
        self.config_entry = entry
        self.client = client
        self._coordinators: list[JdSmartCoordinator] = []
        self._refresh_lock = asyncio.Lock()
        self._retry_cancel: Callable[[], None] | None = None
        self._failure_count = 0
        self._validating_refresh = False
        self._shutdown = False

    def register_coordinator(self, coordinator: JdSmartCoordinator) -> None:
        """Register a coordinator for post-refresh validation."""
        self._coordinators.append(coordinator)

    async def async_handle_auth_failure(
        self,
        failed_tgt: str,
        auth_error: Exception | None = None,
    ) -> bool:
        """Refresh credentials immediately when no retry is already pending."""
        async with self._refresh_lock:
            if failed_tgt != self.client.credentials.tgt:
                return True
            if self._retry_cancel is not None:
                return False
            if self._validating_refresh:
                if auth_error is not None:
                    self.async_schedule_failure(auth_error)
                return False
            return await self._async_refresh_locked()

    async def _async_refresh_locked(self) -> bool:
        """Refresh and persist credentials while holding the refresh lock."""
        try:
            new_tgt, new_cookie = await self.client.async_refresh_token()
        except JdSmartTokenRefreshError as err:
            LOGGER.warning("JD Smart token refresh failed: %s", err)
            self.async_schedule_failure(err)
            return False
        if self._shutdown:
            LOGGER.info("Discarding JD Smart refresh result after shutdown")
            return False

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_TGT: new_tgt,
                CONF_COOKIE: new_cookie,
            },
        )
        self._validating_refresh = True
        return True

    @callback
    def async_schedule_failure(self, err: Exception) -> None:
        """Schedule the next authentication refresh attempt."""
        self._validating_refresh = False
        if self._retry_cancel is not None:
            return
        if self._shutdown:
            # Do not keep scheduling work for an unloaded entry, but let the
            # caller know the retry chain is gone so it can be re-armed after a
            # reload instead of silently polling with dead credentials.
            LOGGER.debug("JD Smart retry not scheduled: entry is shut down")
            return
        self._failure_count += 1
        delay = AUTH_REFRESH_RETRY_DELAYS[
            min(self._failure_count - 1, len(AUTH_REFRESH_RETRY_DELAYS) - 1)
        ]
        retry_at = dt_util.utcnow() + delay
        self._retry_cancel = async_track_point_in_utc_time(
            self.hass,
            self._async_retry_callback,
            retry_at,
        )
        self._async_update_notification(err, retry_at)

    @callback
    def _async_retry_callback(self, _now: datetime) -> None:
        """Start a scheduled authentication refresh attempt."""
        self._retry_cancel = None
        if self._shutdown:
            return
        self.hass.async_create_task(self._async_retry())

    async def _async_retry(self) -> None:
        """Refresh credentials and validate them with a device snapshot."""
        async with self._refresh_lock:
            if self._shutdown:
                LOGGER.debug("JD Smart retry skipped after shutdown")
                return
            refreshed = await self._async_refresh_locked()
        if not refreshed:
            return
        if not self._coordinators:
            # Nothing to validate against yet; make sure a later poll can
            # re-arm the retry instead of leaving the chain dead.
            self.async_schedule_failure(
                JdSmartTokenRefreshError("no coordinator to validate refresh")
            )
            return
        try:
            await self._coordinators[0].async_request_refresh()
        except Exception as err:  # noqa: BLE001
            LOGGER.exception("JD Smart post-refresh validation failed")
            self.async_schedule_failure(err)

    @callback
    def async_mark_recovered(self) -> None:
        """Reset retry state and remove the authentication notification."""
        self._failure_count = 0
        self._validating_refresh = False
        if self._retry_cancel:
            self._retry_cancel()
            self._retry_cancel = None
        self._async_dismiss_notifications()

    @callback
    def _async_dismiss_notifications(self) -> None:
        """Dismiss current and legacy authentication notifications."""
        feed_ids = tuple(coordinator.feed_id for coordinator in self._coordinators)
        for notification_id in auth_refresh_notification_ids(
            self.config_entry.entry_id, feed_ids
        ):
            persistent_notification.async_dismiss(self.hass, notification_id)

    @callback
    def _async_update_notification(
        self,
        err: Exception,
        retry_at: datetime,
    ) -> None:
        """Create or update the single authentication retry notification."""
        reason = str(err) or err.__class__.__name__
        local_retry_at = dt_util.as_local(retry_at).strftime("%Y-%m-%d %H:%M:%S %Z")
        feed_ids = tuple(coordinator.feed_id for coordinator in self._coordinators)
        notification_ids = auth_refresh_notification_ids(
            self.config_entry.entry_id, feed_ids
        )
        for legacy_id in notification_ids[1:]:
            persistent_notification.async_dismiss(self.hass, legacy_id)
        persistent_notification.async_create(
            self.hass,
            (
                "JD Smart authentication refresh failed. "
                f"Attempt: {self._failure_count}. "
                f"Reason: {reason}. "
                f"Next automatic refresh: {local_retry_at}. "
                "You can also open Settings > Devices & services and use "
                "Refresh authentication or enter new authentication data."
            ),
            title="JD Smart authentication refresh retrying",
            notification_id=notification_ids[0],
        )

    @callback
    def async_shutdown(self) -> None:
        """Cancel the pending authentication retry."""
        self._shutdown = True
        if self._retry_cancel:
            self._retry_cancel()
            self._retry_cancel = None


class JdSmartCoordinator(DataUpdateCoordinator[JdSmartSnapshot]):
    """Data coordinator for JD Smart."""

    config_entry: JdSmartConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: JdSmartConfigEntry,
        client: JdSmartClient,
        feed_id: str,
        device_name: str | None,
        device_type: str,
        auth_retry_manager: JdSmartAuthRetryManager,
    ) -> None:
        """Initialize coordinator."""
        self._configured_update_interval = timedelta(
            seconds=_scan_interval_seconds(entry.data)
        )
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._configured_update_interval,
        )
        self.client = client
        self.feed_id = feed_id
        self.device_name = device_name
        self.device_type = device_type
        self.auth_retry_manager = auth_retry_manager
        self._fast_poll_cancel: Callable[[], None] | None = None
        self._consecutive_update_failures = 0
        self._update_failing = False
        self._missing_digest = False
        self._silent_polls = 0
        self.auth_retry_pending = False
        auth_retry_manager.register_coordinator(self)

    async def _async_update_data(self) -> JdSmartSnapshot:
        """Fetch latest snapshot."""
        self.auth_retry_pending = False
        # The endpoint is incremental: it compares the digest we send against
        # the device's current revision and answers with the new digest plus
        # the streams that changed. A response without a digest carries no
        # usable cursor, so after a few of those in a row ask for a full
        # snapshot (empty digest) instead of resending a dead one forever.
        digest = ""
        if self.data is not None and self._silent_polls < RESYNC_AFTER_SILENT_POLLS:
            digest = self.data.digest
        failed_tgt = self.client.credentials.tgt
        try:
            snapshot = await self.client.async_get_snapshot(self.feed_id, digest)
        except JdSmartAuthError as err:
            LOGGER.info("JD Smart snapshot authentication failed; refreshing token")
            if not await self.auth_retry_manager.async_handle_auth_failure(
                failed_tgt, err
            ):
                # Either a retry is already pending or the refresh just failed.
                # Re-arming is a no-op while a timer exists, but it is what keeps
                # the chain alive when the previous timer already fired and
                # nothing else scheduled the next attempt.
                self.auth_retry_manager.async_schedule_failure(err)
                self.auth_retry_pending = True
                raise UpdateFailed(
                    "JD Smart authentication refresh is scheduled"
                ) from err
            try:
                snapshot = await self.client.async_get_snapshot(self.feed_id, digest)
            except JdSmartError as retry_err:
                self.auth_retry_manager.async_schedule_failure(retry_err)
                self.auth_retry_pending = True
                raise UpdateFailed(
                    "JD Smart authentication validation failed"
                ) from retry_err
        except JdSmartCannotConnectError as err:
            if self.data is None:
                raise ConfigEntryNotReady from err
            # Connectivity trouble is a retryable transport failure, not an
            # authentication problem, so it must never escalate to
            # ConfigEntryAuthFailed: Home Assistant stops scheduling refreshes
            # for a coordinator that raises it, and the integration would stay
            # stale until the entry is reloaded. Serve the previous data so a
            # brief outage does not flip every entity to unavailable, and
            # report the underlying reason (DNS, TLS, timeout, HTTP) once per
            # outage so the failure is diagnosable instead of silent.
            self._consecutive_update_failures = 0
            if self._update_failing:
                LOGGER.debug(
                    "JD Smart still cannot reach the cloud; keeping previous "
                    "data: feed_id=%s, error=%s",
                    self.feed_id,
                    err,
                )
            else:
                self._update_failing = True
                LOGGER.warning(
                    "JD Smart could not reach the cloud; keeping previous "
                    "data: feed_id=%s, error=%s",
                    self.feed_id,
                    err,
                )
            return self.data
        except JdSmartError as err:
            await self._async_handle_update_failure(err)
            return self.data

        # Reaching this point means the request itself succeeded, so the current
        # credentials are valid even when the device stays silent. Reset the
        # failure state here: a device that is not reporting must not accumulate
        # unrelated transient errors until they look like an authentication
        # failure, and a pending auth retry must be considered validated.
        if self._update_failing:
            self._update_failing = False
            LOGGER.info(
                "JD Smart update recovered: feed_id=%s, failures=%s",
                self.feed_id,
                self._consecutive_update_failures,
            )
        self._consecutive_update_failures = 0
        self.auth_retry_manager.async_mark_recovered()

        if not snapshot.digest:
            # No cursor came back: the response cannot be trusted as a fresh
            # state. Keep the previous data and request a full snapshot next
            # time, otherwise the same digest is resent on every poll.
            if self.data is not None:
                self._silent_polls += 1
                self._async_note_missing_digest(
                    sent_digest=digest,
                    got_digest=snapshot.digest,
                    stream_count=len(snapshot.streams),
                    from_device_success=snapshot.from_device_success,
                    status=snapshot.status,
                    force_full=self._silent_polls > RESYNC_AFTER_SILENT_POLLS,
                )
                return self.data
            self._async_note_digest_resumed()
            return snapshot

        self._silent_polls = 0
        self._async_note_digest_resumed()
        return snapshot

    async def async_control_streams(self, commands: dict[str, object]) -> None:
        """Control streams and refresh state."""
        failed_tgt = self.client.credentials.tgt
        try:
            snapshot = await self.client.async_control_streams(self.feed_id, commands)
        except JdSmartAuthError as err:
            LOGGER.warning(
                "JD Smart control authentication failed: "
                "feed_id=%s, commands=%s, error=%s",
                self.feed_id,
                commands,
                err,
            )
            try:
                if not await self.auth_retry_manager.async_handle_auth_failure(
                    failed_tgt, err
                ):
                    raise UpdateFailed("JD Smart authentication refresh is scheduled")
                snapshot = await self.client.async_control_streams(
                    self.feed_id,
                    commands,
                )
            except JdSmartError as refresh_err:
                self.auth_retry_manager.async_schedule_failure(refresh_err)
                LOGGER.warning(
                    "JD Smart control failed after token refresh: "
                    "feed_id=%s, commands=%s, error=%s",
                    self.feed_id,
                    commands,
                    refresh_err,
                )
                raise UpdateFailed("Unable to control JD Smart") from refresh_err
            self.auth_retry_manager.async_mark_recovered()
        except JdSmartError as err:
            LOGGER.warning(
                "JD Smart control failed: feed_id=%s, commands=%s, error=%s",
                self.feed_id,
                commands,
                err,
            )
            raise UpdateFailed("Unable to control JD Smart") from err
        if snapshot is not None:
            self.async_set_updated_data(snapshot)
        self.trigger_fast_polling()
        await self.async_request_refresh()

    async def _async_handle_update_failure(self, err: JdSmartError) -> None:
        """Handle repeated update failures without flapping the entities.

        Returning ``UpdateFailed`` makes ``DataUpdateCoordinator`` set
        ``last_update_success = False`` immediately, which flips every
        ``CoordinatorEntity`` to unavailable on the very first hiccup. A single
        transient network error must therefore keep the previous data and stay
        available; only a sustained outage (``UPDATE_AUTH_FAILURE_THRESHOLD``
        consecutive failures) is surfaced, first as a notification and then as
        an unavailable state so the user can tell the integration is broken.
        """
        self._consecutive_update_failures += 1
        self._async_note_update_failure(err)
        if self._consecutive_update_failures < UPDATE_AUTH_FAILURE_THRESHOLD:
            # Transient: keep serving the last known good data.
            return
        LOGGER.warning(
            "JD Smart update failed repeatedly; requesting reauthentication: "
            "feed_id=%s, failures=%s",
            self.feed_id,
            self._consecutive_update_failures,
        )
        self._async_create_reauth_notification()
        raise ConfigEntryAuthFailed from err

    @callback
    def _async_note_update_failure(self, err: JdSmartError) -> None:
        """Log a failed update once per outage instead of on every poll."""
        if self._update_failing:
            LOGGER.debug(
                "JD Smart update still failing; keeping previous data: "
                "feed_id=%s, failures=%s, error=%s",
                self.feed_id,
                self._consecutive_update_failures,
                err,
            )
            return
        self._update_failing = True
        LOGGER.warning(
            "JD Smart update failed; keeping previous data: "
            "feed_id=%s, failures=%s, error=%s",
            self.feed_id,
            self._consecutive_update_failures,
            err,
        )

    @callback
    def _async_note_missing_digest(
        self,
        *,
        sent_digest: str = "",
        got_digest: str = "",
        stream_count: int = 0,
        from_device_success: bool = False,
        status: str = "",
        force_full: bool = False,
    ) -> None:
        """Log a snapshot without a usable digest once per outage."""
        if self._missing_digest:
            LOGGER.debug(
                "JD Smart snapshot carried no digest; keeping previous data: "
                "feed_id=%s, sent_digest=%r, got_digest=%r, streams=%s, "
                "silent_polls=%s",
                self.feed_id,
                sent_digest,
                got_digest,
                stream_count,
                self._silent_polls,
            )
            return
        self._missing_digest = True
        LOGGER.warning(
            "JD Smart snapshot carried no usable digest; keeping previous data: "
            "feed_id=%s, sent_digest=%r, got_digest=%r, streams=%s, "
            "from_device_success=%s, status=%s",
            self.feed_id,
            sent_digest,
            got_digest,
            stream_count,
            from_device_success,
            status,
        )
        if force_full:
            LOGGER.warning(
                "JD Smart kept returning snapshots without a digest for %s polls; "
                "requesting a full snapshot (empty digest) to resync: feed_id=%s",
                self._silent_polls,
                self.feed_id,
            )

    @callback
    def _async_note_digest_resumed(self) -> None:
        """Log recovery after a snapshot stopped carrying a digest."""
        if not self._missing_digest:
            return
        self._missing_digest = False
        LOGGER.info("JD Smart snapshot digests resumed: feed_id=%s", self.feed_id)

    @callback
    def _async_create_reauth_notification(self) -> None:
        """Create a persistent reauth notification."""
        persistent_notification.async_create(
            self.hass,
            (
                "JD Smart could not update the device data several times. "
                "Open Settings > Devices & services and reauthenticate JD Smart."
            ),
            title="JD Smart authentication required",
            notification_id=f"{DOMAIN}_{self.feed_id}_reauth",
        )

    async def async_shutdown(self) -> None:
        """Cancel pending coordinator callbacks."""
        if self._fast_poll_cancel:
            self._fast_poll_cancel()
            self._fast_poll_cancel = None
        # The base class cancels the scheduled refresh and shuts the debouncer
        # down; overriding it without awaiting leaves those running.
        await super().async_shutdown()

    @callback
    def trigger_fast_polling(self) -> None:
        """Temporarily poll faster after a control command."""
        self.update_interval = FAST_POLL_INTERVAL
        if self._fast_poll_cancel:
            self._fast_poll_cancel()
        end = dt_util.utcnow() + FAST_POLL_DURATION
        self._fast_poll_cancel = async_track_point_in_utc_time(
            self.hass, self._reset_polling, end
        )

    @callback
    def _reset_polling(self, _now: datetime) -> None:
        """Reset polling interval to the configured value."""
        self.update_interval = self._configured_update_interval
        self._fast_poll_cancel = None
