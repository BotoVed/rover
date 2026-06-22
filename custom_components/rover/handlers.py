"""Rover message handlers for inbound tp=5/6/8/9."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .commands import build_service_call
from .const import (
    DISPLAY_NAME_MAX_LEN,
    LOGGER_HND,
    ROLE_OWNER,
    ROLE_REGULAR,
    TP_CONFIG,
    TP_FORBIDDEN,
    TP_PING_PONG,
    TP_STATUS,
)
from .state_extractor import extract_state

if TYPE_CHECKING:
    from .registry import RoverRegistry
    from .rns_transport import RoverTransport

_LOGGER = logging.getLogger(LOGGER_HND)


class RoverHandlers:
    """Stateless-ish handlers. State lives in registry; side effects via transport+hass."""

    def __init__(
        self,
        hass: HomeAssistant,
        registry: "RoverRegistry",
        transport: "RoverTransport",
    ) -> None:
        self._hass = hass
        self._registry = registry
        self._transport = transport

    # ---------- tp=5 CMD ----------
    async def handle_cmd(self, source_hash: bytes | None, fields: dict) -> None:
        """Execute a command on a device after authorization check."""
        src_hex = source_hash.hex() if source_hash else None
        if src_hex is None:
            _LOGGER.warning("CMD reject: no source_hash in envelope")
            return

        if not self._registry.is_approved(src_hex):
            _LOGGER.warning("CMD reject: forbidden src=%s...", src_hex[:8])
            await self._transport.send(src_hex, {"tp": TP_FORBIDDEN, "reason": "forbidden"})
            return

        short_id = fields.get("id")
        if not isinstance(short_id, int):
            _LOGGER.warning("CMD reject: missing or invalid id=%r", short_id)
            return

        device = self._registry.get_device(short_id)
        if device is None:
            _LOGGER.warning("CMD reject: unknown short_id=%d", short_id)
            return
        if not device.get("enabled", True):
            _LOGGER.warning("CMD reject: device id=%d disabled", short_id)
            return

        device_type = device["type"]
        entity_id = device["entity_id"]

        try:
            calls = build_service_call(device_type, fields)
        except ValueError as e:
            _LOGGER.warning("CMD reject: build_service_call failed for id=%d: %s", short_id, e)
            return

        _LOGGER.info(
            "CMD src=%s... id=%d entity=%s type=%s calls=%d",
            src_hex[:8], short_id, entity_id, device_type, len(calls)
        )

        for domain, service, data in calls:
            service_data = {"entity_id": entity_id, **data}
            try:
                await self._hass.services.async_call(
                    domain, service, service_data, blocking=False
                )
                _LOGGER.debug("SVC call %s.%s data=%s", domain, service, service_data)
            except Exception:
                _LOGGER.exception("SVC ERROR %s.%s for entity=%s", domain, service, entity_id)

    # ---------- tp=6 PING/PONG ----------
    async def handle_ping(self, source_hash: bytes | None, fields: dict) -> None:
        """Reply to PING with PONG (current hashes). No side effects (DECISIONS 039)."""
        src_hex = source_hash.hex() if source_hash else None
        if src_hex is None:
            return
        if not self._registry.is_approved(src_hex):
            _LOGGER.warning("PING reject: forbidden src=%s...", src_hex[:8])
            await self._transport.send(src_hex, {"tp": TP_FORBIDDEN, "reason": "forbidden"})
            return

        pong = {"tp": TP_PING_PONG, "h": self._registry.get_hashes()}
        _LOGGER.debug("PONG dst=%s... h=%s", src_hex[:8], pong["h"])
        await self._transport.send(src_hex, pong)

    # ---------- tp=8 REQ ----------
    async def handle_req(self, source_hash: bytes | None, fields: dict) -> None:
        """Send requested config section(s). Accepts single string or list."""
        src_hex = source_hash.hex() if source_hash else None
        if src_hex is None:
            return
        if not self._registry.is_approved(src_hex):
            _LOGGER.warning("REQ reject: forbidden src=%s...", src_hex[:8])
            await self._transport.send(src_hex, {"tp": TP_FORBIDDEN, "reason": "forbidden"})
            return

        raw = fields.get("section")
        if isinstance(raw, str):
            sections = [raw]
        elif isinstance(raw, list):
            sections = raw
        else:
            _LOGGER.warning("REQ reject: invalid section=%r", raw)
            return

        hashes = self._registry.get_hashes()
        sent_device = False
        for section in sections:
            if section not in ("m", "u", "a", "d"):
                _LOGGER.warning("REQ skip: unknown section=%r", section)
                continue

            if section == "m":
                data = self._registry.get_meta()
            elif section == "u":
                data = self._registry.all_users()
            elif section == "a":
                data = self._registry.all_areas()
            else:
                data = [
                    {
                        "id": d["short_id"],
                        "n": d["name"],
                        "dt": d["type"],
                        "a": d.get("area_id"),
                    }
                    for d in self._registry.all_devices()
                    if d.get("enabled", True)
                ]

            msg = {"tp": TP_CONFIG, "section": section, "h": hashes[section], "data": data}
            _LOGGER.info(
                "CONFIG dst=%s... section=%s items=%s h=%s",
                src_hex[:8], section,
                len(data) if isinstance(data, list) else "obj",
                hashes[section],
            )
            await self._transport.send(src_hex, msg)

            if section == "d":
                sent_device = True

        if sent_device:
            await self._send_status_snapshot(src_hex)

    async def _send_status_snapshot(self, dst_hex: str) -> None:
        """Build and send tp=2 STATUS with current states of all enabled devices."""
        states = []
        for d in self._registry.all_devices():
            if not d.get("enabled", True):
                continue
            entity_id = d["entity_id"]
            ha_state = self._hass.states.get(entity_id)
            if ha_state is None:
                continue
            try:
                extracted = extract_state(
                    ha_state.state, dict(ha_state.attributes), d["type"]
                )
            except ValueError:
                continue
            states.append({"id": d["short_id"], **extracted})

        msg = {"tp": TP_STATUS, "s": states}
        _LOGGER.info("STATUS dst=%s... items=%d", dst_hex[:8], len(states))
        await self._transport.send(dst_hex, msg)

    async def _send_forbidden(self, dst_hex: str) -> None:
        """Send tp=7 FORBIDDEN to a non-approved remote."""
        await self._transport.send(dst_hex, {"tp": TP_FORBIDDEN, "reason": "forbidden"})

    # ---------- tp=9 REGISTER ----------
    async def handle_register(self, source_hash: bytes | None, fields: dict) -> None:
        """Auto-approve via QR token uid. First remote becomes owner."""
        src_hex = source_hash.hex() if source_hash else None
        if src_hex is None:
            _LOGGER.warning("REGISTER reject: no source_hash")
            return

        if self._registry.is_approved(src_hex):
            _LOGGER.info("REGISTER from already-approved %s... — resending CONFIG", src_hex[:8])
            await self._send_full_config(src_hex)
            return

        uid = fields.get("uid")
        if not isinstance(uid, str) or not uid:
            _LOGGER.warning("REGISTER reject: missing uid, src=%s...", src_hex[:8])
            await self._send_forbidden(src_hex)
            return

        if not self._registry.consume_qr_token(uid):
            _LOGGER.warning("REGISTER reject: invalid uid=%s src=%s...", uid, src_hex[:8])
            await self._send_forbidden(src_hex)
            return

        name = fields.get("name", "Unknown")
        if not isinstance(name, str):
            name = str(name)
        name = name.strip()[:DISPLAY_NAME_MAX_LEN] or "Unknown"

        ver = fields.get("ver")
        _LOGGER.info("REGISTER src=%s... name=%r ver=%r uid=%s", src_hex[:8], name, ver, uid)

        active_count = len(self._registry.all_users())
        role = ROLE_OWNER if active_count == 0 else ROLE_REGULAR

        if not await self._registry.add_pending(src_hex, name):
            _LOGGER.warning("REGISTER: add_pending failed (queue full?)")
            return

        await self._registry.approve_pending(src_hex, role=role)
        _LOGGER.info("REGISTER auto-approved role=%s: %s... name=%r", role, src_hex[:8], name)
        await self._send_full_config(src_hex)

    async def _send_full_config(self, dst_hex: str) -> None:
        """Send all 4 config sections sequentially + STATUS snapshot."""
        hashes = self._registry.get_hashes()
        sections = {
            "m": self._registry.get_meta(),
            "u": self._registry.all_users(),
            "a": self._registry.all_areas(),
            "d": [
                {
                    "id": d["short_id"],
                    "n": d["name"],
                    "dt": d["type"],
                    "a": d.get("area_id"),
                }
                for d in self._registry.all_devices()
                if d.get("enabled", True)
            ],
        }
        for section, data in sections.items():
            msg = {"tp": TP_CONFIG, "section": section, "h": hashes[section], "data": data}
            await self._transport.send(dst_hex, msg)
            _LOGGER.debug("CONFIG dst=%s... section=%s h=%s", dst_hex[:8], section, hashes[section])

        await self._send_status_snapshot(dst_hex)
        pong = {"tp": TP_PING_PONG, "h": self._registry.get_hashes()}
        await self._transport.send(dst_hex, pong)
