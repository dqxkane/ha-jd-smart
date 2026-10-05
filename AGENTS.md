# Repository Guidelines

## Commit Messages

Use Conventional Commits for all commit messages.

Examples:

- `feat: add device discovery config flow`
- `fix: correct fan speed mapping`
- `docs: update setup instructions`

## Project Overview

Home Assistant custom integration (HACS-compatible) for JD Smart air conditioners.
Communicates with JD's undocumented cloud APIs using credentials captured from the mobile app's HTTPS traffic.

- **Domain:** `jd_smart`
- **Min HA version:** 2025.3.0
- **Runtime dependency:** `cryptography` only (beyond HA itself)
- **Language:** Python 3.13+, `from __future__ import annotations` used throughout

## Commands

```bash
# Install test dependencies
uv sync --group test

# Run tests
uv run --group test pytest
```

No linter, formatter, or type checker is configured in CI. `.gitignore` references `.mypy_cache/` and `.ruff_cache/` suggesting ruff/mypy may be used locally but are not enforced.

## CI (`.github/workflows/validate.yml`)

Three jobs run on every push/PR:
1. **Tests** -- `uv sync --locked --group test && uv run --locked --group test pytest`
2. **HACS validation** -- `hacs/action@main` with `category: integration`
3. **Hassfest validation** -- `home-assistant/actions/hassfest@master`

## Project Structure

Single-package repo. All integration code lives under `custom_components/jd_smart/`.

```
custom_components/jd_smart/
  __init__.py      # Entry setup/unload, platform forwarding
  manifest.json    # HA integration manifest
  const.py         # Domain, config keys, defaults, API paths, DEVICE_TYPE_BY_CATEGORY
  api.py           # Full API client (JD Smart + Wangyin + WJLogin crypto)
  config_flow.py   # Config flow UI (manual auth, refresh, add device, reauth)
  coordinator.py   # DataUpdateCoordinator + JdSmartAuthRetryManager
  entity.py        # Base entity class
  climate.py       # Climate platform (HVAC, temp, fan, swing, preset)
  switch.py        # Switch platform (backlight, display, powerful)
  select.py        # Select platform (horizontal swing)
  sensor.py        # Sensor platform (temp, humidity, diagnostics)
  strings.json     # Translation source (English) — compile to translations/*.json
  translations/    # en.json, zh-Hans.json
```

## Architecture Notes

### Stream-based device model
Entities are created only for "streams" present in the device snapshot (key-value pairs like `power`, `mode`, `settemp`, `mark`, `verdir`, `hordir`). This is a dynamic feature-detection pattern — new streams automatically produce new entities.

### Device type routing
Only `category_id = "101001"` (air conditioners) is supported. The `DEVICE_TYPE_BY_CATEGORY` dict in `const.py:102` is the extension point for new device types.

### Authentication flow
The API client (`api.py`) implements multiple cryptographic protocols:
- JD Smart HMAC-SHA1 authorization header signing
- Wangyin (JD Pay) ECDH key exchange with SECP256K1, AES-256-ECB encryption
- WJLogin QQTEA (TEA variant) encryption for token refresh
- Wangyin session is lazily established and cached; decrypt failures trigger a single retry with session reset.

`JdSmartAuthError` is raised for every signal that means "refresh the token", not just HTTP 401:
`JD_SMART_AUTH_ERROR_CODES` (`const.py`) currently covers HTTP 401/403 and the JD Smart
`errorCode`/`status` value `-4` (`登录已过期，请重新登录`). Only `JdSmartAuthError` starts the
token-refresh path in `coordinator.py`; anything else falls through to the reauth threshold.

### Incremental snapshot and the digest cursor
`getDeviceSnapshot_v1` is incremental: the client sends `digest` (the revision it last accepted)
and the server answers with the device's current `digest` plus the streams that changed since then.

`fromDeviceSuccess` is **not** a validity flag. It only reports whether the *device* pushed new
data this round; JD sends `false` for ordinary "nothing changed" polls while still returning an
advanced digest and the full stream set (observed as `streams=11, from_device_success=false`).
Treating it as "reject this response" freezes every entity at its boot value:

- `_async_update_data()` returns `self.data` unchanged, so the digest it sends never advances.
- Every later poll repeats the identical request, and the stale snapshot is reused indefinitely.
- Nothing raises, so nothing is logged as an error, and entities simply stop changing.

The observable symptom is therefore *frozen values with a clean log* (e.g. CO2 stuck at 503 while
the vendor app shows 490). Only a full pull breaks the cycle — which is why re-saving credentials
(forcing a reload, so the coordinator starts with `data is None` and sends `digest=""`) restores
live data.

Responses are therefore accepted whenever they carry a non-empty `digest`. The one genuinely
unusable reply is a snapshot **without** a digest: there is no cursor to advance to, so resending
the old one would repeat the request forever. `RESYNC_AFTER_SILENT_POLLS` (`const.py`) caps the
number of consecutive cursorless replies before the coordinator deliberately sends an empty digest
to resync.

### Failure accounting and entity availability
`JdSmartCoordinator._async_update_data()` resets `_consecutive_update_failures` whenever the
HTTP request itself succeeds — including the cursorless "keep previous data" case.
A device that is not reporting is a device problem, not an authentication problem, so those polls
must not let unrelated transient errors accumulate into a `ConfigEntryAuthFailed`.

`_consecutive_update_failures` counts **consecutive** failures: any poll that reaches the success
path resets it to `0`, so it is not a lifetime or per-hour tally. With the default 5-minute
interval, `UPDATE_AUTH_FAILURE_THRESHOLD = 3` therefore means "three *server-side* errors in a
row". Reaching the threshold requires the request to complete and the API to answer with an
error — a connectivity failure never counts (see the table below).

The threshold also governs entity availability, because HA's contract is unforgiving here:

- `CoordinatorEntity.available` returns `coordinator.last_update_success`.
- Raising `UpdateFailed` from `_async_update_data()` sets `last_update_success = False`
  **on that same poll** and immediately pushes the state, so the very first hiccup would flip
  every entity to *unavailable*. `self.data` is preserved either way.
- Therefore `_async_handle_update_failure()` must **not** raise while the count is below the
  threshold. It logs once per outage (`_async_note_update_failure`, WARNING then DEBUG), serves
  the previous data, and keeps the entities available.
- Only on reaching the threshold does it create a reauth notification and raise
  `ConfigEntryAuthFailed`.

The three failure classes are deliberately kept apart:

| Failure | Counter | Entities | Escalation |
|---|---|---|---|
| `JdSmartCannotConnectError` (DNS, TLS, timeout, HTTP 5xx) | reset to `0` | available, previous data served | never |
| `JdSmartError` (the API answered with an error) | `+1` | available below the threshold | `ConfigEntryAuthFailed` at the threshold |
| `JdSmartAuthError` (expired credentials) | n/a | unavailable | own retry chain, below |

A connectivity failure must **never** escalate: Home Assistant stops scheduling refreshes for a
coordinator that raises `ConfigEntryAuthFailed`, so a transient DNS or network outage would leave
the integration stale until the entry is reloaded. That branch instead logs the underlying reason
(DNS, TLS, timeout, HTTP status — the API client passes an explicit
`ClientTimeout(total=30, sock_connect=10)`, so a stalled connection fails inside the poll period
rather than hanging for aiohttp's 300-second default) once per outage, and serves the previous
data.

The consequence is that a transient network error is invisible in the UI (entities keep their
last values) and nearly invisible in the log (one WARNING). Recovery logs one INFO. That is the
intended trade-off: users asked for "no *unavailable* for a blip", so the only signal for a
long-running *server-side* outage is the notification and the threshold-triggered unavailability.

`JdSmartAuthError` does **not** go through this path. It has its own retry chain (below) and still
marks the entities unavailable, because expired credentials genuinely require user action.

### Auth retry with backoff
`JdSmartAuthRetryManager` (`coordinator.py`) coordinates token refresh across devices sharing one config entry. Escalating delays: 5, 10, 20, 40, 60 minutes. Validates refreshed credentials with a snapshot fetch before clearing failure state.

The retry chain must always stay armed while credentials are bad. Every path that observes an
unrecovered `JdSmartAuthError` has to (re)call `async_schedule_failure()`: the previously armed
timer is cleared to `None` right before it fires, so a branch that only raises `UpdateFailed`
leaves nothing scheduled and the coordinator polls forever with dead credentials until a manual
reload. `async_schedule_failure()` therefore distinguishes "a timer is already armed" (no-op)
from "the entry is shut down" (skip, and never arm new work for an unloaded entry).

A silent device is a *device* problem, not an authentication one. `_async_update_data()` reaching
its success path — including the "device did not respond, keep previous data" branch — resets the
failure state and clears any pending retry via `async_mark_recovered()`. Because that branch
returns the previous snapshot without raising, a long device outage is invisible in the logs
beyond one WARNING and one INFO on recovery; it does not indicate broken credentials.

Any config-flow path that writes credentials must go through `_refresh_and_validate_auth()`
(`config_flow.py`): a WJLogin refresh can return a new A2 that the device API still rejects, so
refresh success alone is not proof of working credentials. Note that WJLogin frequently returns
the *same* A2; `same_token` in `async_refresh_token()` is logged but is not itself a failure.

### Fast polling after control
After a control command, the coordinator temporarily switches to 2-second polling for 10 seconds, then reverts to the interval configured via the options flow.

### Legacy single-device entries
The `_entry_devices()` helper in both `__init__.py` and `config_flow.py` handles old config entries that stored a single `feed_id` at the top level instead of the current `devices` list.

## Bilingual Documentation

README and DISCLAIMER are maintained in both English and Simplified Chinese. Both must be kept in sync. See `.agents/documentation-maintainer.md` for the doc maintenance workflow.

## Testing

- Framework: `pytest-homeassistant-custom-component` (v0.13.232) with `asyncio_mode = "auto"`
- `tests/conftest.py` auto-enables custom integration loading
- Tests use extensive mocking (no real API calls)
- Run a single test file: `uv run --group test pytest tests/test_coordinator.py`

## Translation Workflow

`strings.json` is the source of truth. After editing, regenerate:
- `translations/en.json`
- `translations/zh-Hans.json`

Follow the existing key structure in `strings.json` — translations mirror it exactly.
