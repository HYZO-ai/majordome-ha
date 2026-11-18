"""Cœur de Majordome : planification des analyses, actions, mises à jour sûres."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .analyzers import config as config_analyzer
from .analyzers import logs as logs_analyzer
from .analyzers import updates as updates_analyzer
from .analyzers.zigbee import ZigbeeMonitor
from .const import (
    CAT_ZIGBEE,
    CONF_AUTO_PATCH,
    CONF_NOTIFY_SERVICE,
    CONF_SCAN_INTERVAL,
    CONF_WATCHDOG,
    CONF_Z2M_TOPIC,
    CRITICAL,
    DEFAULT_AUTO_PATCH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WATCHDOG,
    DEFAULT_Z2M_TOPIC,
    RISK_LOW,
    RISK_MEDIUM,
    SEVERITY_ORDER,
    SEVERITY_PENALTY,
    SIGNAL_REPORT_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
    WARNING,
)
from .models import action, issue
from .watchdog import Watchdog

_LOGGER = logging.getLogger(__name__)

VERIFY_DELAY_NO_RESTART = 90  # s
VERIFY_DELAY_AFTER_START = 240  # s


# Actions sûres pour « Tout corriger » : réversibles, jamais de suppression d'automatisation.
BULK_SAFE = (
    "disable_entity",
    "disable_entities",
    "remove_entities",
    "delete_corrupt_db",
    "z2m_options",
    "z2m_ping",
    "set_dns",
    "backup_now",
    "update_all_safe",
    "unban_local",
    "disable_entry",
    "z2m_restart",
)


def _assign_autofix(item: dict[str, Any]) -> None:
    """Choisit l'action appliquée par « Tout corriger » pour un problème."""
    if "autofix" in item:
        return
    acts = item.get("actions", [])
    if item["id"].startswith("entry_"):
        wanted = "disable_entry" if "non chargée" in item["title"] else "reload_entry"
        item["autofix"] = next((a for a in acts if a["action"] == wanted), None)
        return
    item["autofix"] = next((a for a in acts if a["action"] in BULK_SAFE), None)


class Majordome:
    """Objet central, un par instance."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = {"ignored": [], "incidents": [], "update_history": [], "notified": [], "pending_checks": []}
        self.report: dict[str, Any] = {
            "generated": None,
            "score": None,
            "issues": [],
            "updates": [],
            "logs": [],
            "zigbee": {},
            "system": {},
        }
        self.zigbee = ZigbeeMonitor(hass, self)
        self.watchdog = Watchdog(hass, self)
        self._unsubs: list = []
        self._scan_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self.busy: str | None = None
        self._bulk_running = False

    # ---------------------------------------------------------------- options
    def opt(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, default)

    @property
    def z2m_topic(self) -> str:
        return str(self.opt(CONF_Z2M_TOPIC, DEFAULT_Z2M_TOPIC)).strip("/") or DEFAULT_Z2M_TOPIC

    # -------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            self.data.update(stored)
        await self.zigbee.async_start()
        if self.opt(CONF_WATCHDOG, DEFAULT_WATCHDOG):
            self.watchdog.start()
        interval = timedelta(minutes=int(self.opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))
        self._unsubs.append(async_track_time_interval(self.hass, self._scheduled_scan, interval))

        async def _first(_now=None) -> None:
            await self.async_scan()
            await self._async_run_pending_checks()

        if self.hass.state is CoreState.running:
            self._unsubs.append(async_call_later(self.hass, 20, _first))
        else:
            self._unsubs.append(async_call_later(self.hass, VERIFY_DELAY_AFTER_START, _first))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.watchdog.stop()
        await self.zigbee.async_stop()
        await self.store.async_save(self.data)

    @callback
    def async_save_delayed(self) -> None:
        self.store.async_delay_save(lambda: self.data, 10)

    async def _scheduled_scan(self, _now=None) -> None:
        await self.async_scan()
        await self._async_maybe_auto_patch()

    # ------------------------------------------------------------------ scan
    async def async_scan(self) -> dict[str, Any]:
        async with self._scan_lock:
            issues: list[dict[str, Any]] = []
            updates: list[dict] = []
            logs: list[dict] = []
            zigbee: dict[str, Any] = {}
            system: dict[str, Any] = {}

            async def _safe(name, coro):
                try:
                    return await coro
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Analyse « %s » en échec", name)
                    return None

            res = await _safe("mises à jour", updates_analyzer.async_analyze(self.hass, self))
            if res:
                updates, found = res
                issues += found
            try:
                logs, found = logs_analyzer.analyze(self.hass)
                issues += found
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Analyse du journal en échec")
            try:
                zigbee, found = self.zigbee.analyze()
                issues += found
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Analyse Zigbee en échec")
            res = await _safe("configuration", config_analyzer.async_analyze(self.hass, self))
            if res:
                system, found = res
                issues += found

            issues += self._watchdog_issues()
            issues += self._update_history_issues()

            for i in issues:
                _assign_autofix(i)
            ignored = set(self.data.get("ignored", []))
            visible = [i for i in issues if i["id"] not in ignored]
            visible.sort(key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9), i["category"], i["title"]))
            score = 100
            for i in visible:
                score -= SEVERITY_PENALTY.get(i["severity"], 0)
            score = max(0, min(100, score))

            zigbee["incidents"] = list(reversed(self.data.get("incidents", [])[-40:]))
            self.report = {
                "auto_update": self.auto_update_policies(),
                "generated": dt_util.utcnow().isoformat(),
                "score": score,
                "issues": visible,
                "ignored_count": len(issues) - len(visible),
                "updates": updates,
                "logs": logs,
                "zigbee": zigbee,
                "system": system,
                "update_history": list(reversed(self.data.get("update_history", [])[-30:])),
            }
            await self._async_notify_new_critical(visible)
            async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)
            return self.report

    def _watchdog_issues(self) -> list[dict[str, Any]]:
        out = []
        for entity_id, count in self.watchdog.unstable_devices().items():
            state = self.hass.states.get(entity_id)
            name = state.attributes.get("friendly_name", entity_id) if state else entity_id
            out.append(
                issue(
                    f"unstable_{entity_id}",
                    CAT_ZIGBEE,
                    WARNING,
                    f"{name} rate souvent des commandes ({count} fois en 7 jours)",
                    detail="Le chien de garde a dû renvoyer ou n'a pas pu faire passer certaines commandes.",
                    fix="Signal trop faible ou relais manquant : ajoute une prise Zigbee entre lui et le dongle, ou reconfigure-le.",
                    entities=[entity_id],
                )
            )
        return out

    def _update_history_issues(self) -> list[dict[str, Any]]:
        out = []
        for h in self.data.get("update_history", [])[-10:]:
            if h.get("verdict") == "régression" and not h.get("acknowledged"):
                acts = []
                if h.get("can_rollback") and h.get("from"):
                    acts.append(
                        action(
                            "rollback",
                            f"Revenir à {h['from']}",
                            {"entity_id": h["entity_id"], "version": h["from"], "key": h["key"]},
                            confirm=f"Réinstaller {h['title']} en version {h['from']} ?",
                        )
                    )
                acts.append(action("ack_update", "C'est réglé", {"key": h["key"]}))
                out.append(
                    issue(
                        f"regression_{h['key']}",
                        "updates",
                        CRITICAL,
                        f"Problème après la mise à jour de {h['title']}",
                        detail=h.get("details", ""),
                        fix="Reviens à la version précédente, ou restaure la sauvegarde faite juste avant (Paramètres > Système > Sauvegardes).",
                        actions=acts,
                    )
                )
        return out

    # --------------------------------------------------------- notifications
    async def async_notify(self, title: str, message: str, notification_id: str | None = None) -> None:
        from homeassistant.components import persistent_notification  # noqa: PLC0415

        persistent_notification.async_create(
            self.hass, message, title=f"Majordome · {title}", notification_id=notification_id
        )
        service = str(self.opt(CONF_NOTIFY_SERVICE, "") or "").strip()
        if service:
            domain, _, name = service.partition(".")
            if not name:
                domain, name = "notify", domain
            try:
                await self.hass.services.async_call(
                    domain, name, {"title": f"Majordome · {title}", "message": message}, blocking=False
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Notification via %s impossible : %s", service, err)

    async def _async_notify_new_critical(self, issues: list[dict[str, Any]]) -> None:
        notified = set(self.data.get("notified", []))
        current = {i["id"] for i in issues if i["severity"] == CRITICAL}
        new = [i for i in issues if i["severity"] == CRITICAL and i["id"] not in notified]
        for i in new[:3]:
            await self.async_notify(i["title"], f"{i.get('detail') or ''}\n\n➜ {i.get('fix') or ''}".strip(), f"majordome_{i['id']}"[:250])
        # On oublie les problèmes résolus pour pouvoir re-notifier s'ils reviennent
        self.data["notified"] = sorted(current)
        if new or notified != current:
            self.async_save_delayed()

    # ------------------------------------------------------- safe updates
    def _snapshot(self) -> dict[str, Any]:
        failing = [
            e.entry_id
            for e in self.hass.config_entries.async_entries()
            if not e.disabled_by and e.state in (ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY, ConfigEntryState.MIGRATION_ERROR)
        ]
        ent_reg = er.async_get(self.hass)
        unavailable: dict[str, int] = {}
        for st in self.hass.states.async_all():
            if st.state == "unavailable":
                entry = ent_reg.async_get(st.entity_id)
                platform = entry.platform if entry else "?"
                unavailable[platform] = unavailable.get(platform, 0) + 1
        return {"failing": failing, "unavailable": unavailable, "time": time.time()}

    def _compare(self, before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str]:
        problems = []
        new_failing = set(after["failing"]) - set(before["failing"])
        for entry_id in new_failing:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            problems.append(f"L'intégration « {entry.title if entry else entry_id} » ne démarre plus.")
        for platform, count in after["unavailable"].items():
            prev = before["unavailable"].get(platform, 0)
            if platform == "mobile_app":
                continue
            if count - prev >= 3 and count > prev * 1.5:
                problems.append(f"{count - prev} entité(s) « {platform} » sont devenues indisponibles.")
        if problems:
            return "régression", " ".join(problems)
        return "ok", "Tout répond comme avant la mise à jour."

    async def _async_backup(self, label: str) -> bool:
        """Sauvegarde partielle rapide (configuration HA) avant une mise à jour."""
        if self.hass.services.has_service("hassio", "backup_partial"):
            try:
                await self.hass.services.async_call(
                    "hassio",
                    "backup_partial",
                    {"homeassistant": True, "name": f"Majordome – avant {label}"[:60], "compressed": True},
                    blocking=True,
                )
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Sauvegarde avant mise à jour impossible : %s", err)
                return False
        if self.hass.services.has_service("backup", "create_automatic"):
            try:
                await self.hass.services.async_call("backup", "create_automatic", {}, blocking=True)
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Sauvegarde avant mise à jour impossible : %s", err)
        return False

    def _restart_required(self) -> bool:
        reg = ir.async_get(self.hass)
        for (domain, issue_id), item in reg.issues.items():
            if item.active and "restart" in issue_id and domain in ("hacs", "homeassistant", self.entry.domain):
                return True
        return False

    async def async_safe_update(self, entity_id: str, schedule_verify: bool = True) -> dict[str, Any]:
        """Sauvegarde → installation → vérification (ou programmation de la vérification)."""
        async with self._update_lock:
            state = self.hass.states.get(entity_id)
            if state is None or state.state != "on":
                return {"ok": False, "message": "Pas de mise à jour disponible pour cette entité."}
            upd = next((u for u in self.report.get("updates", []) if u["entity_id"] == entity_id), None)
            if upd and upd["risk"] == "blocked":
                return {"ok": False, "message": "Mise à jour bloquée : incompatible avec ta version de Home Assistant."}
            title = state.attributes.get("title") or state.attributes.get("friendly_name") or entity_id
            installed = state.attributes.get("installed_version")
            latest = state.attributes.get("latest_version")
            features = state.attributes.get("supported_features") or 0
            self.busy = f"Mise à jour de {title}"
            async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)
            try:
                before = self._snapshot()
                native_backup = bool(features & updates_analyzer.FEATURE_BACKUP)
                backed_up = native_backup or await self._async_backup(f"{title} {latest}")
                data: dict[str, Any] = {"entity_id": entity_id}
                if native_backup:
                    data["backup"] = True
                await self.hass.services.async_call("update", "install", data, blocking=True)
                key = f"{entity_id}:{latest}:{int(time.time())}"
                record = {
                    "key": key,
                    "time": time.time(),
                    "entity_id": entity_id,
                    "title": title,
                    "from": installed,
                    "to": latest,
                    "backup": backed_up,
                    "can_rollback": bool(features & updates_analyzer.FEATURE_SPECIFIC_VERSION),
                    "verdict": "en attente",
                    "details": "",
                    "before": before,
                }
                self.data.setdefault("update_history", []).append(record)
                self.data["update_history"] = self.data["update_history"][-50:]
                await asyncio.sleep(3)
                needs_restart = self._restart_required()
                record["needs_restart"] = needs_restart
                if needs_restart:
                    record["verdict"] = "redémarrage requis"
                    self.data.setdefault("pending_checks", []).append(key)
                    msg = f"{title} installé ({installed} → {latest}). Un redémarrage est nécessaire ; Majordome vérifiera tout au redémarrage."
                else:
                    if schedule_verify:
                        async_call_later(self.hass, VERIFY_DELAY_NO_RESTART, self._make_verifier(key))
                    msg = f"{title} installé ({installed} → {latest}). Vérification automatique dans 90 s."
                self.async_save_delayed()
                return {"ok": True, "message": msg, "needs_restart": needs_restart, "key": key, "title": title}
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Mise à jour de %s en échec", entity_id)
                return {"ok": False, "message": f"Échec de la mise à jour : {err}"}
            finally:
                if not self._bulk_running:
                    self.busy = None
                async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)

    def _make_verifier(self, key: str):
        async def _verify(_now=None) -> None:
            await self._async_verify_update(key)

        return _verify

    async def _async_verify_update(self, key: str, rescan: bool = True) -> str | None:
        record = next((h for h in self.data.get("update_history", []) if h["key"] == key), None)
        if record is None:
            return None
        verdict, details = self._compare(record.get("before", {"failing": [], "unavailable": {}}), self._snapshot())
        record["verdict"] = verdict
        record["details"] = details
        record.pop("before", None)
        if key in self.data.get("pending_checks", []):
            self.data["pending_checks"].remove(key)
        self.async_save_delayed()
        if verdict == "régression":
            await self.async_notify(
                f"Problème après la mise à jour de {record['title']}",
                details + " Ouvre Majordome pour revenir en arrière.",
                f"majordome_regression_{record['entity_id']}",
            )
        if rescan:
            await self.async_scan()
        return verdict

    async def _async_run_pending_checks(self) -> None:
        for key in list(self.data.get("pending_checks", [])):
            await self._async_verify_update(key)

    def _update_candidates(self, level: str) -> list[dict[str, Any]]:
        """Mises à jour installables : 'low' = correctifs, 'medium' = sans risque élevé."""
        allowed = {RISK_LOW} if level == "low" else {RISK_LOW, RISK_MEDIUM}
        items = [
            u for u in self.report.get("updates", [])
            if u["risk"] in allowed and u["can_install"] and not u["in_progress"]
        ]
        # Home Assistant Core / OS en dernier : leur installation redémarre tout
        items.sort(key=lambda u: u["source"] in ("core", "os", "supervisor"))
        return items

    async def async_update_all_safe(self, level: str = "low") -> dict[str, Any]:
        items = self._update_candidates(level)
        if not items:
            return {"ok": True, "message": "Rien à installer à ce niveau de risque."}
        if self._bulk_running:
            return {"ok": False, "message": "Des mises à jour sont déjà en cours."}
        self.hass.async_create_background_task(self._async_bulk_update(items), "majordome_bulk_update")
        names = ", ".join(u["title"] for u in items)
        return {
            "ok": True,
            "message": f"{len(items)} mise(s) à jour lancée(s) en arrière-plan, une par une avec vérification : {names}. Tu recevras une notification à la fin.",
        }

    async def _async_bulk_update(self, items: list[dict[str, Any]]) -> None:
        """Installe une liste de mises à jour et s'arrête au premier problème détecté."""
        self._bulk_running = True
        done: list[str] = []
        failed: list[str] = []
        restart: list[str] = []
        stopped: str | None = None
        try:
            for idx, upd in enumerate(items, 1):
                self.busy = f"Mises à jour {idx}/{len(items)} : {upd['title']}"
                async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)
                res = await self.async_safe_update(upd["entity_id"], schedule_verify=False)
                if not res.get("ok"):
                    failed.append(upd["title"])
                    continue
                if res.get("needs_restart"):
                    restart.append(upd["title"])
                    continue
                self.busy = f"Vérification après {upd['title']}…"
                async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)
                await asyncio.sleep(60)
                verdict = await self._async_verify_update(res["key"], rescan=False)
                done.append(upd["title"])
                if verdict == "régression":
                    stopped = upd["title"]
                    break
        finally:
            self._bulk_running = False
            self.busy = None
        lines = []
        if done:
            lines.append("Installé et vérifié : " + ", ".join(done) + ".")
        if restart:
            lines.append("Installé, redémarrage nécessaire : " + ", ".join(restart) + ". Majordome vérifiera au redémarrage.")
        if failed:
            lines.append("Échec : " + ", ".join(failed) + ".")
        if stopped:
            lines.append(f"⚠️ Arrêt après {stopped} : un problème a été détecté, les mises à jour suivantes n'ont pas été installées.")
        await self.async_notify("Mises à jour terminées", " ".join(lines) or "Rien n'a été installé.", "majordome_bulk_update")
        await self.async_scan()

    def auto_update_policies(self) -> dict[str, str]:
        """Règles de mise à jour automatique par entité : off / patch / safe."""
        return dict(self.data.get("auto_update", {}))

    async def _async_maybe_auto_patch(self) -> None:
        hour = dt_util.now().hour
        if not 3 <= hour < 5:
            return
        today = dt_util.now().date().isoformat()
        if self.data.get("last_auto_patch") == today or self._bulk_running:
            return
        policies = self.auto_update_policies()
        global_patch = self.opt(CONF_AUTO_PATCH, DEFAULT_AUTO_PATCH)
        items = []
        for u in self.report.get("updates", []):
            policy = policies.get(u["entity_id"])
            if policy is None and global_patch and u["source"] in ("hacs", "addon"):
                policy = "patch"
            if not u["can_install"]:
                continue
            if policy == "patch" and u["risk"] == RISK_LOW:
                items.append(u)
            elif policy == "safe" and u["risk"] in (RISK_LOW, RISK_MEDIUM):
                items.append(u)
        if not items:
            return
        self.data["last_auto_patch"] = today
        self.async_save_delayed()
        items.sort(key=lambda u: u["source"] in ("core", "os", "supervisor"))
        await self._async_bulk_update(items)

    # --------------------------------------------------------------- actions
    async def async_action(self, name: str, params: dict[str, Any], _scan: bool = True) -> dict[str, Any]:
        """Exécute une action demandée depuis le panneau (toujours confirmée côté UI)."""
        hass = self.hass
        if name == "rescan":
            await self.async_scan()
            return {"ok": True, "message": "Analyse terminée."}
        if name == "update":
            return await self.async_safe_update(params["entity_id"])
        if name == "update_all_safe":
            return await self.async_update_all_safe(params.get("level", "low"))
        if name == "fix_all":
            return await self.async_fix_all(params.get("ids", []))
        if name == "set_auto_update":
            policies = self.data.setdefault("auto_update", {})
            for entity_id, policy in (params.get("policies") or {}).items():
                if policy in ("patch", "safe"):
                    policies[entity_id] = policy
                else:
                    policies.pop(entity_id, None)
            self.async_save_delayed()
            count = len(policies)
            return {"ok": True, "message": f"Règles enregistrées : {count} élément(s) en mise à jour automatique (la nuit, entre 3 h et 5 h)."}
        if name == "rollback":
            await hass.services.async_call(
                "update", "install", {"entity_id": params["entity_id"], "version": params["version"]}, blocking=True
            )
            for h in self.data.get("update_history", []):
                if h["key"] == params.get("key"):
                    h["acknowledged"] = True
                    h["verdict"] = "annulée"
            self.async_save_delayed()
            needs = self._restart_required()
            return {"ok": True, "message": f"Version {params['version']} réinstallée." + (" Redémarre Home Assistant pour l'appliquer." if needs else "")}
        if name == "ack_update":
            for h in self.data.get("update_history", []):
                if h["key"] == params.get("key"):
                    h["acknowledged"] = True
            self.async_save_delayed()
        elif name == "ignore":
            ignored = set(self.data.get("ignored", []))
            ignored.add(params["issue_id"])
            self.data["ignored"] = sorted(ignored)
            self.async_save_delayed()
        elif name == "unignore_all":
            self.data["ignored"] = []
            self.async_save_delayed()
        elif name in ("disable_entity", "disable_entities"):
            reg = er.async_get(hass)
            ids = params.get("entity_ids") or [params.get("entity_id")]
            count = 0
            for entity_id in ids:
                if entity_id and reg.async_get(entity_id):
                    reg.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
                    count += 1
            if not _scan:
                return {"ok": True, "message": f"{count} entité(s) désactivée(s)."}
            await self.async_scan()
            return {"ok": True, "message": f"{count} entité(s) désactivée(s). Réactivables dans Paramètres > Entités."}
        elif name == "delete_automation":
            return await self._async_delete_automation(params.get("entity_id"), params.get("config_id"))
        elif name == "z2m_ping":
            await self.zigbee.async_ping(params["device"])
            return {"ok": True, "message": f"Demande envoyée à {params['device']}."}
        elif name == "z2m_reconfigure":
            await self.zigbee.async_reconfigure(params["device"])
            return {"ok": True, "message": f"Reconfiguration de {params['device']} lancée (réveille-le s'il est sur pile)."}
        elif name == "z2m_restart":
            await self.zigbee.async_restart()
            return {"ok": True, "message": "Redémarrage de Zigbee2MQTT demandé."}
        elif name == "z2m_options":
            await self.zigbee.async_set_options(params["options"])
            return {"ok": True, "message": "Option envoyée à Zigbee2MQTT."}
        elif name == "disable_entry":
            await hass.config_entries.async_set_disabled_by(params["entry_id"], ConfigEntryDisabler.USER)
        elif name == "reload_entry":
            await hass.config_entries.async_reload(params["entry_id"])
        elif name == "remove_entities":
            reg = er.async_get(hass)
            removed = 0
            for entity_id in params.get("entity_ids", []):
                st = hass.states.get(entity_id)
                if reg.async_get(entity_id) and (st is None or st.attributes.get("restored")):
                    reg.async_remove(entity_id)
                    removed += 1
            await self.async_scan()
            return {"ok": True, "message": f"{removed} entité(s) supprimée(s)."}
        elif name == "delete_corrupt_db":
            cfg = hass.config.config_dir

            def _delete() -> int:
                count = 0
                for fname in params.get("files", []):
                    if "/" in fname or "\\" in fname or not fname.startswith(config_analyzer.DB_FILE + ".corrupt"):
                        continue
                    path = os.path.join(cfg, fname)
                    if os.path.isfile(path):
                        os.remove(path)
                        count += 1
                return count

            count = await hass.async_add_executor_job(_delete)
            await self.async_scan()
            return {"ok": True, "message": f"{count} fichier(s) supprimé(s)."}
        elif name == "set_dns":
            client = config_analyzer._supervisor_client(hass)  # noqa: SLF001
            if client is None:
                return {"ok": False, "message": "Disponible uniquement sur Home Assistant OS."}
            try:
                from aiohasupervisor.models import DnsOptions  # noqa: PLC0415

                await client.dns.set_options(DnsOptions(servers=list(params["servers"])))
                await client.dns.restart()
            except Exception as err:  # noqa: BLE001
                return {"ok": False, "message": f"Réglage DNS impossible ({err}). À faire via Paramètres > Système > Réseau."}
            return {"ok": True, "message": "DNS de secours configuré : " + ", ".join(params["servers"])}
        elif name == "backup_now":
            if hass.services.has_service("hassio", "backup_full"):
                hass.async_create_task(
                    hass.services.async_call("hassio", "backup_full", {"name": "Majordome – sauvegarde manuelle", "compressed": True}, blocking=True)
                )
                return {"ok": True, "message": "Sauvegarde lancée en arrière-plan."}
            if hass.services.has_service("backup", "create_automatic"):
                hass.async_create_task(hass.services.async_call("backup", "create_automatic", {}, blocking=True))
                return {"ok": True, "message": "Sauvegarde lancée en arrière-plan."}
            return {"ok": False, "message": "Service de sauvegarde introuvable."}
        elif name == "unban_local":
            path = os.path.join(hass.config.config_dir, "ip_bans.yaml")
            targets = {str(ip) for ip in params.get("ips", []) if config_analyzer._is_private(str(ip))}  # noqa: SLF001

            def _unban() -> int:
                if not os.path.exists(path):
                    return 0
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()
                out, skip, removed = [], False, 0
                for ln in lines:
                    if ln and not ln[0].isspace() and ln.strip().endswith(":"):
                        key = ln.strip().rstrip(":").strip("'\"")
                        skip = key in targets
                        removed += int(skip)
                    if not skip:
                        out.append(ln)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(out)
                return removed

            removed = await hass.async_add_executor_job(_unban)
            hass.async_create_task(hass.services.async_call("homeassistant", "restart", {}, blocking=False))
            return {"ok": True, "message": f"{removed} adresse(s) débannie(s). Redémarrage de Home Assistant…"}
        elif name == "restart_ha":
            hass.async_create_task(hass.services.async_call("homeassistant", "restart", {}, blocking=False))
            return {"ok": True, "message": "Redémarrage de Home Assistant…"}
        else:
            return {"ok": False, "message": f"Action inconnue : {name}"}
        if _scan:
            await self.async_scan()
        return {"ok": True, "message": "C'est fait."}

    async def async_fix_all(self, ids: list[str]) -> dict[str, Any]:
        """Applique la correction automatique de chaque problème demandé (désactive, ne supprime jamais)."""
        wanted = set(ids)
        todo = [i for i in self.report.get("issues", []) if i["id"] in wanted and i.get("autofix")]
        # Ce qui redémarre Home Assistant passe en dernier
        todo.sort(key=lambda i: i["autofix"]["action"] in ("unban_local",))
        ok, ko = [], []
        seen: set[str] = set()
        for i in todo:
            fix = i["autofix"]
            sig = fix["action"] + repr(sorted((fix.get("params") or {}).items(), key=str))
            if sig in seen:
                continue
            seen.add(sig)
            try:
                res = await self.async_action(fix["action"], fix.get("params") or {}, _scan=False)
                (ok if res.get("ok", True) else ko).append(i["title"])
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Correction de « %s » impossible : %s", i["title"], err)
                ko.append(i["title"])
        await self.async_scan()
        msg = f"{len(ok)} correction(s) appliquée(s)."
        if ko:
            msg += f" Échec : {', '.join(ko)}."
        return {"ok": not ko, "message": msg}

    async def _async_delete_automation(self, entity_id: str | None, config_id: str | None) -> dict[str, Any]:
        """Supprime définitivement une automatisation de automations.yaml (confirmé côté UI)."""
        hass = self.hass
        if not config_id:
            return {"ok": False, "message": "Cette automatisation n'est pas modifiable depuis l'interface (définie en YAML à la main)."}
        from homeassistant.util.file import write_utf8_file_atomic  # noqa: PLC0415
        from homeassistant.util.yaml import dump, load_yaml  # noqa: PLC0415

        path = hass.config.path("automations.yaml")

        def _delete() -> bool:
            data = load_yaml(path) or []
            if not isinstance(data, list):
                return False
            kept = [a for a in data if str((a or {}).get("id")) != str(config_id)]
            if len(kept) == len(data):
                return False
            write_utf8_file_atomic(path, dump(kept))
            return True

        removed = await hass.async_add_executor_job(_delete)
        if not removed:
            return {"ok": False, "message": "Automatisation introuvable dans automations.yaml."}
        await hass.services.async_call("automation", "reload", {}, blocking=True)
        reg = er.async_get(hass)
        if entity_id and reg.async_get(entity_id):
            reg.async_remove(entity_id)
        await self.async_scan()
        return {"ok": True, "message": "Automatisation supprimée."}
