"""Rover — Remote Over Radio for Home Assistant."""
from __future__ import annotations

__version__ = "0.5.7"

import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, LOGGER_ROOT
from .dispatcher import RoverDispatcher
from .ha_bridge import RoverHABridge
from .handlers import RoverHandlers
from .registry import RoverRegistry
from .rns_transport import RoverTransport
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(LOGGER_ROOT)


class RoverRuntimeData:
    """Runtime data for Rover integration."""

    def __init__(self) -> None:
        self.registry: RoverRegistry | None = None
        self.transport: RoverTransport | None = None
        self.handlers: RoverHandlers | None = None
        self.dispatcher: RoverDispatcher | None = None
        self.bridge: RoverHABridge | None = None
        self.identity_hash: str | None = None
        self._registry_unsub = None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """No YAML setup."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rover from a config entry."""
    runtime = RoverRuntimeData()

    # 1. Registry
    runtime.registry = RoverRegistry(hass)
    await runtime.registry.async_load()

    # 2. RNS config directory
    config_dir = os.path.join(
        hass.config.config_dir, "custom_components", DOMAIN, ".reticulum"
    )
    await hass.async_add_executor_job(lambda: os.makedirs(config_dir, exist_ok=True))

    # 3. Inbound routing callback — dispatcher is built below
    async def _route_inbound(source_hash: bytes | None, fields: dict) -> None:
        if runtime.dispatcher is None:
            _LOGGER.warning("INBOUND dropped: dispatcher not yet wired")
            return
        await runtime.dispatcher.dispatch(source_hash, fields)

    # 4. Transport (RNS + LXMF)
    tcp_port = runtime.registry.get_meta().get("tcp_port", 4242)
    runtime.transport = RoverTransport(
        hass=hass,
        config_dir=config_dir,
        on_message=_route_inbound,
        tcp_port=tcp_port,
    )
    try:
        identity_hash = await runtime.transport.async_start()
        runtime.identity_hash = identity_hash
        _LOGGER.info("Rover server identity hash: %s", identity_hash)
    except Exception:
        _LOGGER.exception("Rover transport failed to start; integration will not work")
        return False

    # 5. Handlers + Dispatcher
    runtime.handlers = RoverHandlers(hass, runtime.registry, runtime.transport)
    runtime.dispatcher = RoverDispatcher(runtime.handlers)

    # 6. HA bridge — state_changed → PUSH
    runtime.bridge = RoverHABridge(hass, runtime.registry, runtime.transport)
    await runtime.bridge.async_start()

    # 7. Registry hash-change → broadcast PONG (DECISIONS 038)
    def _on_registry_changed(section: str) -> None:
        if runtime.bridge is None:
            return
        hass.async_create_task(runtime.bridge.broadcast_pong())

    runtime.registry.set_on_changed(_on_registry_changed)
    runtime._registry_unsub = _on_registry_changed

    # 8. Debug services
    await async_register_services(hass, runtime)

    entry.runtime_data = runtime
    _LOGGER.info("Rover %s setup complete", __version__)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Rover config entry. RNS persists until HA restart."""
    runtime: RoverRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None:
        return True

    try:
        async_unregister_services(hass)
    except Exception:
        _LOGGER.exception("Error unregistering services")

    if runtime.registry is not None:
        try:
            runtime.registry.set_on_changed(None)
        except Exception:
            _LOGGER.exception("Error clearing registry callback")

    if runtime.bridge is not None:
        try:
            await runtime.bridge.async_stop()
        except Exception:
            _LOGGER.exception("Error stopping HA bridge")

    if runtime.transport is not None:
        try:
            await runtime.transport.shutdown()
        except Exception:
            _LOGGER.exception("Error in transport.shutdown (RNS singleton stays alive)")

    _LOGGER.info("Rover unloaded (RNS singleton persists until HA restart)")
    return True
