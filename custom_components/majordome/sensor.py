"""Capteurs Majordome (utilisables dans les tableaux de bord et automatisations)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CRITICAL, DOMAIN, RISK_LOW, SIGNAL_REPORT_UPDATED, VERSION, WARNING


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    md = hass.data[DOMAIN]["core"]
    async_add_entities([ScoreSensor(md, entry), IssuesSensor(md, entry), UpdatesSensor(md, entry), ZigbeeOfflineSensor(md, entry)])


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, md, entry: ConfigEntry, key: str, name: str, icon: str) -> None:
        self.md = md
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Majordome",
            manufacturer="Majordome",
            model="Assistant de maintenance",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_REPORT_UPDATED, self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.md.report.get("score") is not None


class ScoreSensor(_Base):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"

    def __init__(self, md, entry) -> None:
        super().__init__(md, entry, "score", "Score de santé", "mdi:heart-pulse")

    @property
    def native_value(self) -> int | None:
        return self.md.report.get("score")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"generated": self.md.report.get("generated"), "busy": self.md.busy}


class IssuesSensor(_Base):
    def __init__(self, md, entry) -> None:
        super().__init__(md, entry, "issues", "Problèmes", "mdi:alert-circle-outline")

    @property
    def native_value(self) -> int:
        return len(self.md.report.get("issues", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        issues = self.md.report.get("issues", [])
        return {
            "critical": sum(1 for i in issues if i["severity"] == CRITICAL),
            "warning": sum(1 for i in issues if i["severity"] == WARNING),
            "top": [i["title"] for i in issues[:5]],
        }


class UpdatesSensor(_Base):
    def __init__(self, md, entry) -> None:
        super().__init__(md, entry, "updates", "Mises à jour", "mdi:update")

    @property
    def native_value(self) -> int:
        return len(self.md.report.get("updates", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ups = self.md.report.get("updates", [])
        return {
            "safe": sum(1 for u in ups if u["risk"] == RISK_LOW),
            "risky": sum(1 for u in ups if u["risk"] != RISK_LOW),
        }


class ZigbeeOfflineSensor(_Base):
    def __init__(self, md, entry) -> None:
        super().__init__(md, entry, "zigbee_offline", "Appareils Zigbee hors ligne", "mdi:zigbee")

    @property
    def native_value(self) -> int:
        devs = (self.md.report.get("zigbee") or {}).get("devices", [])
        return sum(1 for d in devs if d["availability"] == "offline")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        devs = (self.md.report.get("zigbee") or {}).get("devices", [])
        return {"devices": [d["name"] for d in devs if d["availability"] == "offline"][:20]}
