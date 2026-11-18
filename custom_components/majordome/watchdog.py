"""Chien de garde Zigbee : vérifie qu'une lampe a bien obéi, sinon réessaie."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

if TYPE_CHECKING:
    from .core import Majordome

_LOGGER = logging.getLogger(__name__)

WATCHED_DOMAINS = ("light", "switch")
WATCHED_SERVICES = {"turn_on": "on", "turn_off": "off"}
CHECK_DELAY = 6  # secondes
MAX_INCIDENTS = 200


class Watchdog:
    """Détecte les commandes Zigbee perdues et les renvoie une fois."""

    def __init__(self, hass: HomeAssistant, md: "Majordome") -> None:
        self.hass = hass
        self.md = md
        self._unsub = None
        self._own_contexts: set[str] = set()

    def start(self) -> None:
        if self._unsub is None:
            self._unsub = self.hass.bus.async_listen(
                EVENT_CALL_SERVICE, self._on_call, event_filter=self._filter
            )

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _filter(self, event_data) -> bool:
        return (
            event_data.get("domain") in WATCHED_DOMAINS
            and event_data.get("service") in WATCHED_SERVICES
        )

    def _is_zigbee(self, entity_id: str) -> str | None:
        """Retourne 'z2m', 'zha' ou None."""
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None:
            return None
        if entry.platform == "zha":
            return "zha"
        if entry.platform == "mqtt" and entry.device_id:
            dev = dr.async_get(self.hass).async_get(entry.device_id)
            if dev and any(str(i[1]).startswith("zigbee2mqtt_") for i in dev.identifiers):
                return "z2m"
        return None

    def _z2m_name(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        if not entry or not entry.device_id:
            return None
        dev = dr.async_get(self.hass).async_get(entry.device_id)
        if not dev:
            return None
        ieee = next(
            (str(i[1])[len("zigbee2mqtt_") :] for i in dev.identifiers if str(i[1]).startswith("zigbee2mqtt_")),
            None,
        )
        for name, d in self.md.zigbee.devices.items():
            if d.get("ieee_address") == ieee:
                return name
        return dev.name

    @callback
    def _on_call(self, event: Event) -> None:
        if event.context.id in self._own_contexts:
            self._own_contexts.discard(event.context.id)
            return
        data = event.data
        service_data: dict[str, Any] = dict(data.get("service_data") or {})
        ids = service_data.get("entity_id")
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",")]
        if not ids:
            return
        expected = WATCHED_SERVICES[data["service"]]
        for entity_id in ids:
            kind = self._is_zigbee(entity_id)
            if not kind:
                continue
            params = {k: v for k, v in service_data.items() if k != "entity_id"}
            self._schedule_check(data["domain"], data["service"], entity_id, expected, params, kind, attempt=1)

    def _schedule_check(self, domain, service, entity_id, expected, params, kind, attempt) -> None:
        async def _check(_now) -> None:
            await self._async_verify(domain, service, entity_id, expected, params, kind, attempt)

        async_call_later(self.hass, CHECK_DELAY, _check)

    async def _async_verify(self, domain, service, entity_id, expected, params, kind, attempt) -> None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state == expected:
            if attempt > 1:
                self._record(entity_id, kind, "rattrapé", f"Commande « {service} » perdue puis renvoyée avec succès.")
            return
        name = state.attributes.get("friendly_name", entity_id)
        if state.state == "unavailable":
            self._record(entity_id, kind, "injoignable", f"{name} est indisponible : la commande n'a pas pu partir.")
            await self._try_wake(entity_id, kind)
            return
        if attempt == 1:
            ctx = Context()
            self._own_contexts.add(ctx.id)
            try:
                await self.hass.services.async_call(
                    domain, service, {"entity_id": entity_id, **params}, blocking=False, context=ctx
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Renvoi impossible pour %s : %s", entity_id, err)
                return
            self._schedule_check(domain, service, entity_id, expected, params, kind, attempt=2)
            return
        self._record(entity_id, kind, "échec", f"{name} n'a pas obéi à « {service} », même après un second essai.")
        await self._try_wake(entity_id, kind)
        await self.md.async_notify(
            f"{name} ne répond pas",
            f"La commande « {service} » n'a pas abouti malgré un second essai. "
            "Vérifie son alimentation (interrupteur mural) ou ouvre Majordome > Zigbee.",
            notification_id=f"majordome_watchdog_{entity_id}",
        )

    async def _try_wake(self, entity_id: str, kind: str) -> None:
        if kind != "z2m":
            return
        name = self._z2m_name(entity_id)
        if name:
            try:
                await self.md.zigbee.async_ping(name)
            except Exception:  # noqa: BLE001
                pass

    def _record(self, entity_id: str, kind: str, result: str, message: str) -> None:
        incidents: list[dict[str, Any]] = self.md.data.setdefault("incidents", [])
        incidents.append(
            {"time": time.time(), "entity_id": entity_id, "stack": kind, "result": result, "message": message}
        )
        del incidents[:-MAX_INCIDENTS]
        self.md.async_save_delayed()
        _LOGGER.info("Majordome watchdog : %s", message)

    def unstable_devices(self, days: int = 7, threshold: int = 3) -> dict[str, int]:
        """Entités qui ont eu au moins `threshold` incidents récents."""
        since = time.time() - days * 86400
        counts: dict[str, int] = {}
        for inc in self.md.data.get("incidents", []):
            if inc["time"] >= since and inc["result"] in ("échec", "injoignable", "rattrapé"):
                counts[inc["entity_id"]] = counts.get(inc["entity_id"], 0) + 1
        return {e: c for e, c in counts.items() if c >= threshold}
