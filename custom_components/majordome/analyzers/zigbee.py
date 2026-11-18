"""Surveillance Zigbee : Zigbee2MQTT (via MQTT) et ZHA."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..const import CAT_ZIGBEE, CRITICAL, INFO, WARNING
from ..models import action, issue

if TYPE_CHECKING:
    from ..core import Majordome

_LOGGER = logging.getLogger(__name__)

LQI_WEAK = 40
BATTERY_LOW = 15
RECOMMENDED_CHANNELS = (15, 20, 25)


def _loads(payload: Any) -> Any:
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "ignore")
    if not isinstance(payload, str):
        return payload
    payload = payload.strip()
    if not payload:
        return None
    if payload[0] in "{[":
        try:
            return json.loads(payload)
        except ValueError:
            return None
    return payload


class ZigbeeMonitor:
    """Écoute Zigbee2MQTT en direct et analyse ZHA."""

    def __init__(self, hass: HomeAssistant, md: "Majordome") -> None:
        self.hass = hass
        self.md = md
        self.base = md.z2m_topic
        self.bridge_state: str | None = None
        self.info: dict[str, Any] | None = None
        self.devices: dict[str, dict[str, Any]] = {}  # friendly_name -> device
        self.availability: dict[str, str] = {}
        self.telemetry: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._unsub = None
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------------- MQTT
    async def async_start(self) -> None:
        """Démarre l'écoute dès que MQTT est prêt (sans bloquer le démarrage)."""
        self._task = self.hass.async_create_background_task(
            self._async_subscribe(), "majordome_zigbee_subscribe"
        )

    async def _async_subscribe(self) -> None:
        if not self.hass.config_entries.async_entries("mqtt"):
            return
        try:
            from homeassistant.components import mqtt  # noqa: PLC0415

            if not await mqtt.async_wait_for_mqtt_client(self.hass):
                return
            self._unsub = await mqtt.async_subscribe(
                self.hass, f"{self.base}/#", self._on_message, qos=0, encoding=None
            )
            _LOGGER.debug("Majordome écoute %s/#", self.base)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Majordome : écoute Zigbee2MQTT impossible : %s", err)

    async def async_stop(self) -> None:
        """Arrête l'écoute."""
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._task and not self._task.done():
            self._task.cancel()

    @callback
    def _on_message(self, msg) -> None:
        topic: str = msg.topic
        rest = topic[len(self.base) + 1 :]
        if not rest:
            return
        if rest.startswith("bridge/"):
            sub = rest[7:]
            data = _loads(msg.payload)
            if sub == "state":
                self.bridge_state = data.get("state") if isinstance(data, dict) else data
            elif sub == "info" and isinstance(data, dict):
                self.info = data
            elif sub == "devices" and isinstance(data, list):
                self.devices = {
                    d.get("friendly_name"): d for d in data if isinstance(d, dict) and d.get("friendly_name")
                }
            elif sub == "event" and isinstance(data, dict):
                kind = data.get("type")
                if kind in ("device_leave", "device_joined", "device_interview"):
                    self.events.append(
                        {"time": time.time(), "type": kind, "data": data.get("data", {})}
                    )
                    self.events = self.events[-50:]
            return
        if rest.endswith("/availability"):
            name = rest[: -len("/availability")]
            data = _loads(msg.payload)
            state = data.get("state") if isinstance(data, dict) else data
            if isinstance(state, str):
                self.availability[name] = state
            return
        if rest.endswith(("/set", "/get")) or "/set/" in rest:
            return
        data = _loads(msg.payload)
        if not isinstance(data, dict):
            return
        tel = self.telemetry.setdefault(rest, {})
        tel["ts"] = time.time()
        for key in ("linkquality", "battery", "last_seen", "battery_low", "update"):
            if key in data:
                tel[key] = data[key]

    # ------------------------------------------------------------- Actions
    async def async_publish(self, topic: str, payload: Any) -> None:
        """Publie un message MQTT."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        body = payload if isinstance(payload, str) else json.dumps(payload)
        await mqtt.async_publish(self.hass, f"{self.base}/{topic}", body)

    async def async_ping(self, name: str) -> None:
        """Demande l'état à l'appareil (le réveille s'il est routeur)."""
        await self.async_publish(f"{name}/get", {"state": ""})

    async def async_reconfigure(self, name: str) -> None:
        await self.async_publish("bridge/request/device/configure", {"id": name})

    async def async_restart(self) -> None:
        await self.async_publish("bridge/request/restart", "")

    async def async_set_options(self, options: dict[str, Any]) -> None:
        await self.async_publish("bridge/request/options", {"options": options})

    # ------------------------------------------------------------ Analyse
    @property
    def z2m_present(self) -> bool:
        return self.info is not None or bool(self.devices) or self.bridge_state is not None

    def z2m_device_ids(self) -> set[str]:
        """IEEE des appareils Z2M (pour reconnaître leurs entités)."""
        return {str(d.get("ieee_address")) for d in self.devices.values() if d.get("ieee_address")}

    def device_rows(self) -> list[dict[str, Any]]:
        """Liste des appareils pour le panneau."""
        rows: list[dict[str, Any]] = []
        now = time.time()
        for name, dev in self.devices.items():
            if dev.get("type") == "Coordinator":
                continue
            tel = self.telemetry.get(name, {})
            definition = dev.get("definition") or {}
            avail = self.availability.get(name)
            rows.append(
                {
                    "source": "Zigbee2MQTT",
                    "name": name,
                    "ieee": dev.get("ieee_address"),
                    "model": definition.get("model") or dev.get("model_id"),
                    "vendor": definition.get("vendor") or dev.get("manufacturer"),
                    "type": dev.get("type"),
                    "power": dev.get("power_source"),
                    "availability": avail or "unknown",
                    "lqi": tel.get("linkquality"),
                    "battery": tel.get("battery"),
                    "last_seen": tel.get("last_seen"),
                    "seen_ago": int(now - tel["ts"]) if "ts" in tel else None,
                    "interviewed": dev.get("interview_completed", True),
                    "supported": dev.get("supported", True),
                    "disabled": dev.get("disabled", False),
                }
            )
        rows.extend(self._zha_rows())
        order = {"offline": 0, "unknown": 1, "online": 2}
        rows.sort(key=lambda r: (order.get(r["availability"], 1), r["lqi"] if r["lqi"] is not None else 999))
        return rows

    def _zha_rows(self) -> list[dict[str, Any]]:
        entries = self.hass.config_entries.async_entries("zha")
        if not entries or entries[0].state is not ConfigEntryState.LOADED:
            return []
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        rows = []
        for dev in dr.async_entries_for_config_entry(dev_reg, entries[0].entry_id):
            if dev.via_device_id is None and (dev.model or "").lower().startswith(("ezsp", "znp", "coordinator")):
                continue
            ents = er.async_entries_for_device(ent_reg, dev.id)
            states = [self.hass.states.get(e.entity_id) for e in ents if not e.disabled_by]
            states = [s for s in states if s is not None]
            if not states:
                continue
            online = any(s.state != "unavailable" for s in states)
            lqi = None
            for s in states:
                if s.entity_id.endswith("_lqi"):
                    try:
                        lqi = int(float(s.state))
                    except ValueError:
                        pass
            rows.append(
                {
                    "source": "ZHA",
                    "name": dev.name_by_user or dev.name,
                    "ieee": next((i[1] for i in dev.identifiers if i[0] == "zha"), None),
                    "model": dev.model,
                    "vendor": dev.manufacturer,
                    "type": None,
                    "power": None,
                    "availability": "online" if online else "offline",
                    "lqi": lqi,
                    "battery": None,
                    "last_seen": None,
                    "seen_ago": None,
                    "interviewed": True,
                    "supported": True,
                    "disabled": False,
                }
            )
        return rows

    def summary(self) -> dict[str, Any]:
        info = self.info or {}
        coord = info.get("coordinator") or {}
        network = info.get("network") or {}
        routers = sum(1 for d in self.devices.values() if d.get("type") == "Router")
        ends = sum(1 for d in self.devices.values() if d.get("type") == "EndDevice")
        zha_entries = self.hass.config_entries.async_entries("zha")
        return {
            "z2m": self.z2m_present,
            "base_topic": self.base,
            "bridge_state": self.bridge_state,
            "version": info.get("version"),
            "coordinator_type": coord.get("type"),
            "coordinator_revision": (coord.get("meta") or {}).get("revision"),
            "channel": network.get("channel"),
            "pan_id": network.get("pan_id"),
            "routers": routers,
            "end_devices": ends,
            "zha": bool(zha_entries),
            "zha_state": zha_entries[0].state.value if zha_entries else None,
            "events": self.events[-15:],
        }

    def analyze(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Retourne (données Zigbee pour le panneau, problèmes)."""
        issues: list[dict[str, Any]] = []
        rows = self.device_rows()
        summary = self.summary()
        zha_entries = self.hass.config_entries.async_entries("zha")
        zha_entry = zha_entries[0] if zha_entries else None

        # --- Conflit ZHA / Zigbee2MQTT
        if zha_entry and self.z2m_present and not zha_entry.disabled_by:
            if zha_entry.state is ConfigEntryState.LOADED:
                issues.append(
                    issue(
                        "zigbee_double_stack",
                        CAT_ZIGBEE,
                        CRITICAL,
                        "ZHA et Zigbee2MQTT tournent en même temps",
                        detail="Deux piles Zigbee actives. Si elles partagent le même dongle, c'est la cause classique de lampes qui ne répondent plus.",
                        fix="Garde une seule pile. Si tu n'as qu'un seul dongle, désactive ZHA.",
                        actions=[
                            action(
                                "disable_entry",
                                "Désactiver ZHA",
                                {"entry_id": zha_entry.entry_id},
                                confirm="Désactiver l'intégration ZHA ? (réactivable à tout moment)",
                            )
                        ],
                    )
                )
            else:
                issues.append(
                    issue(
                        "zigbee_zha_leftover",
                        CAT_ZIGBEE,
                        WARNING,
                        "ZHA est configurée mais ne démarre pas",
                        detail=(
                            "Zigbee2MQTT gère déjà ton réseau Zigbee. ZHA essaie quand même de "
                            "démarrer à chaque redémarrage et peut tenter d'ouvrir le même dongle."
                        ),
                        fix="Désactive ZHA pour éviter tout conflit sur le dongle.",
                        actions=[
                            action(
                                "disable_entry",
                                "Désactiver ZHA",
                                {"entry_id": zha_entry.entry_id},
                                confirm="Désactiver l'intégration ZHA ? (réactivable à tout moment)",
                            )
                        ],
                    )
                )

        if not self.z2m_present:
            return {"summary": summary, "devices": rows}, issues

        # --- Pont Zigbee2MQTT
        if self.bridge_state and self.bridge_state != "online":
            issues.append(
                issue(
                    "z2m_offline",
                    CAT_ZIGBEE,
                    CRITICAL,
                    "Zigbee2MQTT est hors ligne",
                    detail="Plus aucun appareil Zigbee ne peut être commandé.",
                    fix="Redémarre le module Zigbee2MQTT et regarde son journal.",
                    actions=[action("z2m_restart", "Redémarrer Zigbee2MQTT", confirm="Redémarrer Zigbee2MQTT ?")],
                )
            )

        info = self.info or {}
        cfg = info.get("config") or {}
        avail_cfg = cfg.get("availability")
        avail_on = avail_cfg is True or (isinstance(avail_cfg, dict) and avail_cfg.get("enabled", False))
        if info and not avail_on:
            issues.append(
                issue(
                    "z2m_availability_off",
                    CAT_ZIGBEE,
                    WARNING,
                    "Zigbee2MQTT ne surveille pas la disponibilité des appareils",
                    detail="Sans cette option, une lampe déconnectée reste affichée « allumée » ou « éteinte » au lieu d'« indisponible ». Majordome ne peut pas la détecter.",
                    fix="Active la disponibilité dans Zigbee2MQTT.",
                    actions=[
                        action(
                            "z2m_options",
                            "Activer la disponibilité",
                            {"options": {"availability": {"enabled": True}}},
                            confirm="Activer la surveillance de disponibilité dans Zigbee2MQTT ? (Z2M redémarrera si nécessaire)",
                        )
                    ],
                )
            )
        adv = cfg.get("advanced") or {}
        if info and adv.get("last_seen", "disable") == "disable":
            issues.append(
                issue(
                    "z2m_last_seen_off",
                    CAT_ZIGBEE,
                    INFO,
                    "Zigbee2MQTT n'enregistre pas « vu pour la dernière fois »",
                    fix="Active last_seen pour savoir depuis quand un appareil ne répond plus.",
                    actions=[
                        action(
                            "z2m_options",
                            "Activer last_seen",
                            {"options": {"advanced": {"last_seen": "ISO_8601"}}},
                            confirm="Activer last_seen dans Zigbee2MQTT ?",
                        )
                    ],
                )
            )

        # --- Firmware coordinateur
        coord = info.get("coordinator") or {}
        rev = str((coord.get("meta") or {}).get("revision") or "")
        ctype = str(coord.get("type") or "")
        if rev.isdigit() and len(rev) == 8 and int(rev) < 20230507:
            issues.append(
                issue(
                    "z2m_coordinator_fw",
                    CAT_ZIGBEE,
                    WARNING,
                    "Firmware du dongle Zigbee ancien",
                    detail=f"Coordinateur {ctype}, firmware {rev}. Les firmwares récents corrigent beaucoup de pertes de messages.",
                    fix="Mets à jour le firmware du dongle (outil du fabricant ou flasheur web).",
                )
            )

        # --- Canal
        channel = (info.get("network") or {}).get("channel")
        if channel == 11:
            issues.append(
                issue(
                    "z2m_channel",
                    CAT_ZIGBEE,
                    INFO,
                    "Canal Zigbee 11 : chevauche le Wi-Fi",
                    detail="Le canal 11 est en plein sur le canal Wi-Fi 1. Les canaux 15, 20 ou 25 sont plus tranquilles.",
                    fix="Changer de canal oblige parfois à réappairer : à ne faire que si tu as beaucoup de pertes.",
                )
            )

        # --- Maillage
        routers = summary["routers"]
        ends = summary["end_devices"]
        if ends >= 6 and routers < 3:
            issues.append(
                issue(
                    "z2m_mesh_weak",
                    CAT_ZIGBEE,
                    WARNING,
                    f"Maillage Zigbee faible ({routers} routeur(s) pour {ends} appareils sur pile)",
                    fix="Ajoute des prises ou ampoules Zigbee secteur réparties dans la maison : elles relaient le signal.",
                )
            )

        # --- Appareils
        offline = [r for r in rows if r["availability"] == "offline" and not r["disabled"]]
        for r in offline:
            is_router = r["type"] == "Router" or r["power"] == "Mains (single phase)"
            acts = []
            if r["source"] == "Zigbee2MQTT":
                acts = [
                    action("z2m_ping", "Réveiller", {"device": r["name"]}),
                    action("z2m_reconfigure", "Reconfigurer", {"device": r["name"]}),
                ]
            issues.append(
                issue(
                    f"zb_offline_{r['ieee'] or r['name']}",
                    CAT_ZIGBEE,
                    CRITICAL if is_router else WARNING,
                    f"{r['name']} ne répond plus",
                    detail=(
                        f"{r['vendor'] or ''} {r['model'] or ''} via {r['source']}. "
                        + ("Appareil secteur : il sert aussi de relais, son absence peut couper d'autres appareils." if is_router else "Appareil sur pile : vérifie la pile.")
                    ).strip(),
                    fix="Vérifie qu'il est alimenté (interrupteur mural !), puis essaie « Réveiller ». S'il reste muet, réappaire-le.",
                    actions=acts,
                    extra={"device": r["name"]},
                )
            )
        weak = [r for r in rows if r["lqi"] is not None and r["lqi"] < LQI_WEAK and r["availability"] != "offline"]
        if weak:
            issues.append(
                issue(
                    "zb_weak_signal",
                    CAT_ZIGBEE,
                    WARNING if len(weak) >= 3 else INFO,
                    f"{len(weak)} appareil(s) Zigbee avec un signal faible",
                    detail=", ".join(f"{r['name']} (LQI {r['lqi']})" for r in weak[:10]),
                    fix="Un appareil secteur (prise Zigbee) entre eux et le dongle stabilisera la liaison.",
                )
            )
        low_bat = [r for r in rows if isinstance(r["battery"], (int, float)) and r["battery"] <= BATTERY_LOW]
        if low_bat:
            issues.append(
                issue(
                    "zb_low_battery",
                    CAT_ZIGBEE,
                    WARNING,
                    f"{len(low_bat)} pile(s) Zigbee presque vide(s)",
                    detail=", ".join(f"{r['name']} ({r['battery']} %)" for r in low_bat[:10]),
                    fix="Change les piles avant que les appareils décrochent.",
                )
            )
        bad_interview = [r for r in rows if not r["interviewed"]]
        if bad_interview:
            issues.append(
                issue(
                    "zb_interview",
                    CAT_ZIGBEE,
                    WARNING,
                    "Appairage incomplet",
                    detail=", ".join(r["name"] for r in bad_interview[:10]),
                    fix="Ces appareils n'ont pas fini leur appairage : certaines fonctions manquent. Réappaire-les près du dongle.",
                )
            )
        return {"summary": summary, "devices": rows}, issues
