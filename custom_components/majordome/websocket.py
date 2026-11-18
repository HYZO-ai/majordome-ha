"""API websocket utilisée par le panneau et la carte."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .analyzers.updates import async_release_notes
from .const import DOMAIN


def _md(hass: HomeAssistant):
    return hass.data.get(DOMAIN, {}).get("core")


def _report(md) -> dict[str, Any]:
    rep = dict(md.report)
    rep["busy"] = md.busy
    return rep


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_report)
    websocket_api.async_register_command(hass, ws_scan)
    websocket_api.async_register_command(hass, ws_chat)
    websocket_api.async_register_command(hass, ws_action)
    websocket_api.async_register_command(hass, ws_notes)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "majordome/report"})
@websocket_api.async_response
async def ws_report(hass: HomeAssistant, connection, msg) -> None:
    md = _md(hass)
    if md is None:
        connection.send_error(msg["id"], "not_loaded", "Majordome n'est pas chargé")
        return
    connection.send_result(msg["id"], _report(md))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "majordome/scan"})
@websocket_api.async_response
async def ws_scan(hass: HomeAssistant, connection, msg) -> None:
    md = _md(hass)
    if md is None:
        connection.send_error(msg["id"], "not_loaded", "Majordome n'est pas chargé")
        return
    await md.async_scan()
    connection.send_result(msg["id"], _report(md))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "majordome/chat", vol.Required("text"): str})
@websocket_api.async_response
async def ws_chat(hass: HomeAssistant, connection, msg) -> None:
    md = _md(hass)
    if md is None:
        connection.send_error(msg["id"], "not_loaded", "Majordome n'est pas chargé")
        return
    from .assistant import Assistant  # noqa: PLC0415

    answer = await Assistant(hass, md).async_handle(msg["text"], connection.context(msg))
    connection.send_result(msg["id"], answer)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "majordome/action",
        vol.Required("action"): str,
        vol.Optional("params", default={}): dict,
    }
)
@websocket_api.async_response
async def ws_action(hass: HomeAssistant, connection, msg) -> None:
    md = _md(hass)
    if md is None:
        connection.send_error(msg["id"], "not_loaded", "Majordome n'est pas chargé")
        return
    try:
        result = await md.async_action(msg["action"], msg.get("params") or {})
    except Exception as err:  # noqa: BLE001
        result = {"ok": False, "message": f"Erreur : {err}"}
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "majordome/release_notes", vol.Required("entity_id"): str})
@websocket_api.async_response
async def ws_notes(hass: HomeAssistant, connection, msg) -> None:
    notes = await async_release_notes(hass, msg["entity_id"])
    connection.send_result(msg["id"], {"notes": notes[:20000]})
