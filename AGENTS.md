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

### Auth retry with backoff
`JdSmartAuthRetryManager` (`coordinator.py`) coordinates token refresh across devices sharing one config entry. Escalating delays: 5, 10, 20, 40, 60 minutes. Validates refreshed credentials with a snapshot fetch before clearing failure state.

### Fast polling after control
After a control command, the coordinator temporarily switches to 2-second polling for 10 seconds, then reverts to the default 60-second interval.

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
