# Rover — HA custom component for mesh control

## Quick reference

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check custom_components/ tests/
```

## Architecture

- **Three repos:** Rover (HA backend, Python), Rover-Card (Lovelace, LitElement), Rover-App (Android, Kotlin)
- **Stack:** Reticulum (RNS) + LXMF — mesh routing, E2E encryption, store-and-forward
- **Protocol types (`tp=`)**: `2` STATUS, `3` PUSH, `4` CONFIG, `5` CMD, `6` PING/PONG, `7` FORBIDDEN, `8` REQ, `9` REGISTER
- **Section hashes**: 4 short hex hashes — `m` (meta), `u` (users), `a` (areas), `d` (devices)
- **No passwords**: auth via RNS Identity (Ed25519/X25519). First registered remote becomes `owner`.
- **No YAML setup**: config_flow only, single_config_entry
- **commands.py / state_extractor.py** — pure functions, testable without HA mocks
- **PONG** is sent proactively on any section hash change + every 8s as keepalive

## Source layout

```
custom_components/rover/
  __init__.py      — setup/unload, wires all components
  const.py          — protocol constants, type defs (SW/LT/CV/...)
  config_flow.py    — single-step config flow
  options_flow.py   — multi-step menu: general/network/devices/users/pending/test
  registry.py       — device/user meta storage (Store-based)
  rns_transport.py  — RNS + LXMF server (TCP on port 4242)
  dispatcher.py     — routes inbound messages to handlers
  handlers.py       — handles CMD, PING, REQ, REGISTER
  ha_bridge.py      — subscribes to HA state_changed, broadcasts PUSH
  commands.py       — cmd_fields → HA service calls (pure)
  state_extractor.py — HA state+attrs → protocol fields (pure)
  services.py       — HA debug services
  codec.py          — msgpack encode/decode
tests/
  conftest.py       — mocks RNS/LXMF/voluptuous at sys.modules level
  test_*.py         — one per module
```

## Test gotchas

- **RNS/LXMF not installed in CI** — `conftest.py` mocks them via `sys.modules` *before* any rover import. If you add a module with top-level RNS/LXMF imports, add corresponding mocks in conftest.
- `voluptuous` also mocked locally (only present on HAOS).
- Pure-function modules (`commands.py`, `state_extractor.py`) test without HA mocks.
- `homeassistant` IS installed in CI (`pip install homeassistant` in test workflow).

## Code style

- Ruff: `line-length = 100`, `target-version = "py312"`
- All files: `from __future__ import annotations`
- Relative imports only: `from .module import X`
- `async def` / `await` throughout; blocking ops via `hass.async_add_executor_job`

## Version bumps

Three files must match:
- `custom_components/rover/__init__.py`: `__version__ = "X.Y.Z"`
- `custom_components/rover/manifest.json`: `"version": "X.Y.Z"`
- `pyproject.toml`: `version = "X.Y.Z"`

Git tag must be clean semver (e.g. `0.5.6`), **no `v` prefix** — HACS rejects non-standard tags.

## HAOS test host

Credentials in `.env`:
```
HAOS_HOST=192.168.1.114  HAOS_PORT=222
HAOS_USER=root           HAOS_PASS=775Ho
```

```bash
# Check logs for rover
sshpass -p "$HAOS_PASS" ssh -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -p "$HAOS_PORT" "$HAOS_USER@$HAOS_HOST" \
  "ha core logs | grep -i rover | tail -20"

# Restart HA
sshpass -p "$HAOS_PASS" ssh -p "$HAOS_PORT" "$HAOS_USER@$HAOS_HOST" "ha core restart"

# Check installed files
sshpass -p "$HAOS_PASS" ssh -p "$HAOS_PORT" "$HAOS_USER@$HAOS_HOST" \
  "ls -la /config/custom_components/rover/"
```

## Release procedure

1. Bump version in 3 files, commit, push to `main`
2. Build ZIP from inside `custom_components/rover/` — files must be flat (no `rover/` prefix):
   ```bash
   cd custom_components/rover && zip -r ../../rover.zip . -x "__pycache__/*" "*.pyc"
   ```
3. Create clean semver tag: `git tag X.Y.Z && git push origin X.Y.Z`
4. CI creates the Release automatically (`.github/workflows/release.yml`)

## Z-release monitoring

Weekly HACS validation runs via `.github/workflows/validate.yml` (Sunday cron).
