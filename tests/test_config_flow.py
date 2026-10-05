"""Tests for JD Smart authentication configuration flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jd_smart.config_flow import JdSmartAcConfigFlow
from custom_components.jd_smart.const import (
    CONF_COOKIE,
    CONF_FEED_ID,
    CONF_SCAN_INTERVAL,
    CONF_TGT,
    DEFAULT_APP_VERSION,
    DEFAULT_CHANNEL,
    DEFAULT_DEVICE_ID,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_PLATFORM,
    DEFAULT_PLATFORM_VERSION,
    DEFAULT_USER_AGENT,
    DOMAIN,
    auth_refresh_notification_ids,
)

AUTH_FORM = {
    CONF_COOKIE: "cookie",
    CONF_TGT: "tgt",
    CONF_DEVICE_ID: DEFAULT_DEVICE_ID,
    "platform": DEFAULT_PLATFORM,
    "app_version": DEFAULT_APP_VERSION,
    "device_model": DEFAULT_DEVICE_MODEL,
    "platform_version": DEFAULT_PLATFORM_VERSION,
    "channel": DEFAULT_CHANNEL,
    "user_agent": DEFAULT_USER_AGENT,
}


def _options_entry(hass) -> MockConfigEntry:
    """Add a fully populated config entry for options flow tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-id",
        data={**AUTH_FORM, CONF_FEED_ID: "feed-id"},
    )
    entry.add_to_hass(hass)
    return entry


async def test_manual_auth_update_clears_retry_notification(hass) -> None:
    """A successful manual auth update clears the background retry notice."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-id",
        data={
            CONF_COOKIE: "old-cookie",
            CONF_TGT: "old-tgt",
            CONF_FEED_ID: "feed-id",
        },
    )
    entry.add_to_hass(hass)
    flow = JdSmartAcConfigFlow()
    flow.hass = hass

    with (
        patch.object(flow, "_async_current_entries", return_value=[entry]),
        patch.object(
            hass.config_entries,
            "async_reload",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.jd_smart.config_flow.persistent_notification.async_dismiss"
        ) as dismiss_notification,
    ):
        await flow._async_update_auth_entries(
            {CONF_COOKIE: "new-cookie", CONF_TGT: "new-tgt"}
        )

    assert dismiss_notification.call_args_list == [
        ((hass, notification_id), {})
        for notification_id in auth_refresh_notification_ids("entry-id", ("feed-id",))
    ]


async def test_options_interval_change_skips_token_refresh(hass) -> None:
    """Editing the polling interval must not re-run the account token refresh."""
    entry = _options_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    with (
        patch(
            "custom_components.jd_smart.config_flow._refresh_and_validate_auth",
            new=AsyncMock(),
        ) as refresh_and_validate,
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {**AUTH_FORM, CONF_SCAN_INTERVAL: "60"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    refresh_and_validate.assert_not_called()
    assert entry.data[CONF_SCAN_INTERVAL] == 60
    assert entry.data[CONF_COOKIE] == "cookie"


async def test_options_auth_change_is_refreshed_and_validated(hass) -> None:
    """Editing a credential refreshes and validates it before saving."""
    entry = _options_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    with (
        patch(
            "custom_components.jd_smart.config_flow._refresh_and_validate_auth",
            new=AsyncMock(),
        ) as refresh_and_validate,
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {**AUTH_FORM, CONF_COOKIE: "new-cookie"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    refresh_and_validate.assert_awaited_once()
    assert entry.data[CONF_COOKIE] == "new-cookie"
