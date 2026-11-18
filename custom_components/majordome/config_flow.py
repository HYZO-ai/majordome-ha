"""Assistant de configuration de Majordome."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    CONF_AUTO_PATCH,
    CONF_NOTIFY_SERVICE,
    CONF_SCAN_INTERVAL,
    CONF_WATCHDOG,
    CONF_Z2M_TOPIC,
    DEFAULT_AUTO_PATCH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WATCHDOG,
    DEFAULT_Z2M_TOPIC,
    DOMAIN,
)


class MajordomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ajout de l'intégration."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Majordome", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MajordomeOptionsFlow()


class MajordomeOptionsFlow(OptionsFlow):
    """Réglages."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        notify_services = sorted(
            f"notify.{name}" for name in self.hass.services.async_services().get("notify", {})
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=1440)
                ),
                vol.Optional(CONF_WATCHDOG, default=opts.get(CONF_WATCHDOG, DEFAULT_WATCHDOG)): bool,
                vol.Optional(CONF_AUTO_PATCH, default=opts.get(CONF_AUTO_PATCH, DEFAULT_AUTO_PATCH)): bool,
                vol.Optional(CONF_Z2M_TOPIC, default=opts.get(CONF_Z2M_TOPIC, DEFAULT_Z2M_TOPIC)): str,
                vol.Optional(CONF_NOTIFY_SERVICE, default=opts.get(CONF_NOTIFY_SERVICE, "")): vol.In(
                    ["", *notify_services]
                )
                if notify_services
                else str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
