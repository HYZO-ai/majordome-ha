"""Assistant conversationnel 100 % local (aucune IA externe, aucune licence).

Fonctionnement : détection d'intention par mots-clés français, recherche floue
des appareils, diagnostic à partir des données de Majordome. Les ordres de
pilotage (« allume le salon ») sont confiés à l'agent local intégré de Home
Assistant (Assist), lui aussi 100 % local.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .analyzers.logs import get_records
from .const import CRITICAL, INFO, RISK_BLOCKED, RISK_HIGH, RISK_LOW, RISK_MEDIUM, WARNING
from .models import action, normalize

if TYPE_CHECKING:
    from .core import Majordome

_LOGGER = logging.getLogger(__name__)

SEV_ICON = {CRITICAL: "🔴", WARNING: "🟠", INFO: "🔵"}
RISK_TXT = {RISK_LOW: "🟢 faible", RISK_MEDIUM: "🟠 moyen", RISK_HIGH: "🔴 élevé", RISK_BLOCKED: "⛔ bloquée"}

INTENTS: list[tuple[str, re.Pattern]] = [
    ("help", re.compile(r"\b(aide|help|que sais tu|tu sais faire|commandes?)\b")),
    ("updates", re.compile(r"\b(mise?s? a jour|maj|update|patch|version|upgrade)\b")),
    ("errors", re.compile(r"\b(erreurs?|logs?|journal|bug|plante|crash)\b")),
    ("zigbee", re.compile(r"\b(zigbee|z2m|zigbee2mqtt|zha|maillage|dongle|coordinateur|lqi)\b")),
    ("optimize", re.compile(r"\b(optimis\w*|nettoy\w*|accelere\w*|lent\w*|alleg\w*|base de donnees|historique)\b")),
    ("notifications", re.compile(r"\b(notifications?|reparations?|alertes?)\b")),
    ("backup", re.compile(r"\b(sauvegardes?|backup)\b")),
    ("health", re.compile(r"\b(sante|bilan|etat general|score|tout va bien|ca va|resume|probleme?s?)\b")),
]
CONTROL_VERBS = re.compile(
    r"^(allume|eteins?|eteint|ouvre|ferme|active|desactive|mets?|regle|lance|demarre|arrete|baisse|monte|augmente|diminue|change)\b"
)
DIAG_WORDS = re.compile(
    r"\b(pourquoi|marche pas|fonctionne pas|repond pas|ne s allume|ne s eteint|panne|bug|probleme|souci|diagnostic|verifie|que se passe)\b"
)
STOP_WORDS = {
    "le", "la", "les", "l", "de", "du", "des", "d", "un", "une", "mon", "ma", "mes", "ton", "ta", "pourquoi",
    "est", "ce", "que", "qui", "ne", "pas", "plus", "marche", "fonctionne", "repond", "s", "allume", "eteint",
    "il", "elle", "y", "a", "au", "aux", "dans", "sur", "et", "ou", "probleme", "souci", "panne", "verifie",
    "diagnostic", "se", "passe", "avec", "lampe", "lumiere", "ampoule", "capteur", "prise",
}


def _md(lines: list[str]) -> str:
    return "\n".join(lines)


class Assistant:
    """Moteur de dialogue."""

    def __init__(self, hass: HomeAssistant, md: "Majordome") -> None:
        self.hass = hass
        self.md = md

    async def async_handle(self, text: str, context: Context | None = None) -> dict[str, Any]:
        raw = (text or "").strip()
        norm = normalize(raw)
        if not norm:
            return self._help()

        if CONTROL_VERBS.match(norm):
            res = await self._converse(raw, context)
            if res:
                return res

        intent = next((name for name, rx in INTENTS if rx.search(norm)), None)
        entity = self._find_entity(norm)

        if entity and (DIAG_WORDS.search(norm) or intent in (None, "health")):
            return await self._diagnose(entity)
        if intent == "help":
            return self._help()
        if intent == "updates":
            return self._updates()
        if intent == "errors":
            return self._errors()
        if intent == "zigbee":
            return self._zigbee()
        if intent == "optimize":
            return self._optimize()
        if intent == "notifications":
            return self._notifications()
        if intent == "backup":
            return self._backup()
        if intent == "health":
            return self._health()
        if entity:
            return await self._diagnose(entity)

        res = await self._converse(raw, context)
        if res:
            return res
        return self._help(prefix="Je n'ai pas compris. ")

    # ------------------------------------------------------------ helpers
    def _report(self) -> dict[str, Any]:
        return self.md.report or {}

    def _find_entity(self, norm: str) -> str | None:
        words = [w for w in norm.split() if w not in STOP_WORDS and len(w) > 1]
        if not words:
            return None
        query = " ".join(words)
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        best: tuple[float, str] | None = None

        def _hit(word: str, tokens: set[str]) -> float:
            if word in tokens:
                return 1.0
            if len(word) >= 4 and any(t.startswith(word) or word.startswith(t) for t in tokens if len(t) >= 4):
                return 0.8
            return 0.0

        for st in self.hass.states.async_all():
            domain = st.entity_id.split(".")[0]
            if domain in ("update", "automation", "script", "zone", "person", "sun", "event", "tts", "conversation"):
                continue
            name = normalize(st.attributes.get("friendly_name", ""))
            if not name:
                continue
            extra = ""
            entry = ent_reg.async_get(st.entity_id)
            if entry and entry.device_id:
                dev = dev_reg.async_get(entry.device_id)
                if dev:
                    extra = normalize(f"{dev.name_by_user or ''} {dev.name or ''} {dev.model or ''}")
            tokens = set(name.split()) | set(extra.split())
            overlap = sum(_hit(w, tokens) for w in words) / len(words)
            ratio = difflib.SequenceMatcher(None, query, name).ratio()
            score = overlap * 0.7 + ratio * 0.3
            if domain in ("light", "switch", "cover", "climate", "fan", "vacuum", "lock", "media_player"):
                score += 0.05
            if best is None or score > best[0]:
                best = (score, st.entity_id)
        if best and best[0] >= 0.45:
            return best[1]
        return None

    async def _converse(self, text: str, context: Context | None) -> dict[str, Any] | None:
        try:
            from homeassistant.components import conversation  # noqa: PLC0415

            result = await conversation.async_converse(
                self.hass, text, None, context or Context(), language="fr", agent_id="conversation.home_assistant"
            )
            resp = result.response
            speech = (resp.speech or {}).get("plain", {}).get("speech", "")
            if str(getattr(resp, "response_type", "")).lower().endswith("error"):
                return None
            if speech:
                return {"text": f"🏠 {speech}", "actions": []}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Assist local indisponible : %s", err)
        return None

    # ------------------------------------------------------------ intents
    def _help(self, prefix: str = "") -> dict[str, Any]:
        return {
            "text": _md(
                [
                    prefix + "Je suis **Majordome**, l'assistant de maintenance de ta maison. Je tourne entièrement chez toi, sans IA externe.",
                    "",
                    "Tu peux me demander :",
                    "- **« Fais le bilan »** : état de santé global",
                    "- **« Quelles mises à jour sont sûres ? »**",
                    "- **« Quelles erreurs ? »** : le journal expliqué",
                    "- **« Comment va le Zigbee ? »**",
                    "- **« Pourquoi la lampe du salon ne marche pas ? »** : diagnostic d'un appareil",
                    "- **« Optimise »** : nettoyage et réglages conseillés",
                    "- **« Allume la cuisine »** : je transmets à Assist (local)",
                ]
            ),
            "actions": [],
        }

    def _health(self) -> dict[str, Any]:
        rep = self._report()
        issues = rep.get("issues", [])
        score = rep.get("score")
        if score is None:
            return {"text": "Première analyse en cours… Réessaie dans une minute.", "actions": [action("rescan", "Analyser maintenant")]}
        crit = [i for i in issues if i["severity"] == CRITICAL]
        warn = [i for i in issues if i["severity"] == WARNING]
        mood = "👌 Tout roule." if score >= 90 else "🙂 Quelques points à regarder." if score >= 70 else "⚠️ Il y a du travail."
        lines = [f"**Score de santé : {score}/100**. {mood}", f"{len(crit)} critique(s), {len(warn)} avertissement(s), {len(issues) - len(crit) - len(warn)} info(s).", ""]
        for i in (crit + warn)[:6]:
            lines.append(f"- {SEV_ICON[i['severity']]} {i['title']}")
        acts = []
        for i in (crit + warn)[:3]:
            acts.extend(i.get("actions", [])[:1])
        return {"text": _md(lines), "actions": acts[:4]}

    def _updates(self) -> dict[str, Any]:
        ups = self._report().get("updates", [])
        if not ups:
            return {"text": "✅ Tout est à jour.", "actions": []}
        lines = [f"**{len(ups)} mise(s) à jour disponible(s)** :", ""]
        for u in ups:
            lines.append(f"- {RISK_TXT.get(u['risk'], u['risk'])} — **{u['title']}** {u['installed']} → {u['latest']}")
            if u["reasons"]:
                lines.append(f"  ↳ {u['reasons'][0]}")
        safe = [u for u in ups if u["risk"] == RISK_LOW and u["can_install"]]
        lines += ["", "Les correctifs « faible » peuvent partir sans crainte. Pour les autres, ouvre l'onglet Mises à jour."]
        acts = [action("update_all_safe", f"Installer les {len(safe)} correctifs sûrs", confirm="Sauvegarde puis installation des correctifs à faible risque ?")] if safe else []
        return {"text": _md(lines), "actions": acts}

    def _errors(self) -> dict[str, Any]:
        groups = self._report().get("logs", [])
        if not groups:
            return {"text": "✅ Aucune erreur dans le journal depuis le dernier démarrage.", "actions": []}
        lines = [f"**{sum(g['count'] for g in groups)} erreurs/avertissements**, regroupés en {len(groups)} causes :", ""]
        for g in groups[:7]:
            lines.append(f"- {SEV_ICON.get(g['severity'], '•')} **{g['title']}** ({g['count']}×) — {', '.join(g['integrations'][:4])}")
            lines.append(f"  ↳ {g['fix']}")
        return {"text": _md(lines), "actions": []}

    def _zigbee(self) -> dict[str, Any]:
        z = self._report().get("zigbee", {})
        s = z.get("summary", {})
        devs = z.get("devices", [])
        if not s.get("z2m") and not devs:
            return {"text": "Je ne vois ni Zigbee2MQTT ni ZHA actif. Vérifie le nom du topic MQTT dans les options de Majordome.", "actions": []}
        offline = [d for d in devs if d["availability"] == "offline"]
        weak = [d for d in devs if d.get("lqi") is not None and d["lqi"] < 40]
        lines = [
            f"**Zigbee2MQTT {s.get('version') or ''}** — pont {s.get('bridge_state') or 'inconnu'}, canal {s.get('channel') or '?'}, "
            f"{s.get('routers', 0)} routeurs / {s.get('end_devices', 0)} appareils sur pile.",
            f"{len(devs)} appareil(s) suivis, **{len(offline)} hors ligne**, {len(weak)} signal faible.",
        ]
        if s.get("zha"):
            lines.append(f"ZHA est aussi configurée (état : {s.get('zha_state')}).")
        acts = []
        for d in offline[:5]:
            lines.append(f"- 🔴 {d['name']} ({d.get('model') or '?'}) ne répond plus")
            if d["source"] == "Zigbee2MQTT":
                acts.append(action("z2m_ping", f"Réveiller {d['name']}", {"device": d["name"]}))
        inc = z.get("incidents", [])[:3]
        if inc:
            lines += ["", "Derniers incidents du chien de garde :"]
            for i in inc:
                when = dt_util.as_local(dt_util.utc_from_timestamp(i["time"])).strftime("%d/%m %H:%M")
                lines.append(f"- {when} : {i['message']}")
        return {"text": _md(lines), "actions": acts[:4]}

    def _optimize(self) -> dict[str, Any]:
        rep = self._report()
        items = [i for i in rep.get("issues", []) if i["category"] in ("config", "system")]
        sysinfo = rep.get("system", {})
        lines = ["**Optimisation**", ""]
        if sysinfo.get("db_size_mb") is not None:
            lines.append(f"- Base de l'historique : {sysinfo['db_size_mb']} Mo")
        if sysinfo.get("disk_free_gb") is not None:
            lines.append(f"- Disque libre : {sysinfo['disk_free_gb']:.1f} Go")
        chatty = sysinfo.get("chatty", [])[:3]
        if chatty:
            lines.append("- Plus bavards : " + ", ".join(f"{c['entity_id']} ({c['per_day']}/j)" for c in chatty))
        lines.append("")
        if not items:
            lines.append("✅ Rien à optimiser de ce côté.")
        acts = []
        for i in items[:8]:
            lines.append(f"- {SEV_ICON[i['severity']]} {i['title']}")
            acts.extend(i.get("actions", [])[:1])
        return {"text": _md(lines), "actions": acts[:4]}

    def _notifications(self) -> dict[str, Any]:
        items = [i for i in self._report().get("issues", []) if i["category"] in ("repairs", "system")]
        if not items:
            return {"text": "✅ Aucune réparation en attente côté Home Assistant.", "actions": []}
        lines = ["**Réparations et alertes système :**", ""] + [f"- {SEV_ICON[i['severity']]} {i['title']}" for i in items[:10]]
        return {"text": _md(lines), "actions": [action("open", "Ouvrir les réparations", {"path": "/config/repairs"})]}

    def _backup(self) -> dict[str, Any]:
        last = self._report().get("system", {}).get("last_backup")
        if last:
            when = dt_util.parse_datetime(last)
            txt = f"Dernière sauvegarde : **{dt_util.as_local(when).strftime('%d/%m/%Y %H:%M')}**." if when else f"Dernière sauvegarde : {last}"
        else:
            txt = "Je ne trouve aucune sauvegarde."
        return {"text": txt, "actions": [action("backup_now", "Sauvegarder maintenant", confirm="Lancer une sauvegarde complète ?")]}

    async def _diagnose(self, entity_id: str) -> dict[str, Any]:
        hass = self.hass
        st = hass.states.get(entity_id)
        if st is None:
            return {"text": f"Je ne trouve pas {entity_id}.", "actions": []}
        name = st.attributes.get("friendly_name", entity_id)
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        entry = ent_reg.async_get(entity_id)
        dev = dev_reg.async_get(entry.device_id) if entry and entry.device_id else None
        since = dt_util.as_local(st.last_changed).strftime("%d/%m %H:%M")
        lines = [f"**Diagnostic : {name}** (`{entity_id}`)", ""]
        lines.append(f"- État : **{st.state}** depuis le {since}")
        if entry:
            lines.append(f"- Intégration : {entry.platform}")
        if dev:
            lines.append(f"- Appareil : {dev.manufacturer or ''} {dev.model or ''}".rstrip())
        acts: list[dict[str, Any]] = []
        verdict: list[str] = []

        # Zigbee2MQTT
        z2m_name = self.md.watchdog._z2m_name(entity_id) if entry and entry.platform == "mqtt" else None  # noqa: SLF001
        zdev = self.md.zigbee.devices.get(z2m_name) if z2m_name else None
        if zdev:
            tel = self.md.zigbee.telemetry.get(z2m_name, {})
            avail = self.md.zigbee.availability.get(z2m_name, "inconnue")
            lines.append(f"- Zigbee2MQTT : « {z2m_name} », {zdev.get('type')}, disponibilité **{avail}**")
            if tel.get("linkquality") is not None:
                lqi = tel["linkquality"]
                lines.append(f"- Qualité du lien (LQI) : {lqi} " + ("✅" if lqi >= 80 else "🟠 moyen" if lqi >= 40 else "🔴 faible"))
                if lqi < 40:
                    verdict.append("Le signal est faible : une prise Zigbee secteur entre lui et le dongle réglerait le problème.")
            if "ts" in tel:
                ago = int(time.time() - tel["ts"])
                lines.append(f"- Dernier message reçu il y a {ago // 60} min")
            if avail == "offline":
                verdict.append("Zigbee2MQTT le voit hors ligne : il est probablement privé de courant (interrupteur mural coupé ?) ou hors de portée.")
            acts += [
                action("z2m_ping", "Réveiller", {"device": z2m_name}),
                action("z2m_reconfigure", "Reconfigurer", {"device": z2m_name}),
            ]
        elif entry and entry.platform == "zha":
            lines.append("- Réseau : ZHA")

        # Incidents du chien de garde
        incs = [i for i in self.md.data.get("incidents", []) if i["entity_id"] == entity_id][-5:]
        if incs:
            lines.append(f"- Chien de garde : {len(incs)} incident(s) récent(s), dernier : {incs[-1]['message']}")
            if len(incs) >= 3:
                verdict.append("Il rate régulièrement des commandes : liaison Zigbee instable.")

        # Journal
        needles = {entity_id, name.lower()}
        if entry:
            needles.add(entry.platform)
        if z2m_name:
            needles.add(z2m_name.lower())
        related = []
        for rec in get_records(hass):
            msg = " ".join(str(m) for m in rec.get("message", [])) if isinstance(rec.get("message"), list) else str(rec.get("message"))
            low = msg.lower()
            if entity_id in low or name.lower() in low or (z2m_name and z2m_name.lower() in low):
                related.append(msg[:200])
        if related:
            lines.append(f"- Journal : {len(related)} message(s) le concernent, ex. « {related[0]} »")

        if st.state == "unavailable":
            if not verdict:
                verdict.append("L'entité est indisponible : l'appareil ne communique plus avec Home Assistant.")
            if entry and entry.config_entry_id:
                acts.append(action("reload_entry", "Recharger l'intégration", {"entry_id": entry.config_entry_id}))
        lines.append("")
        lines.append("**Conclusion** : " + (" ".join(verdict) if verdict else "rien d'anormal détecté de mon côté. ✅"))
        return {"text": _md(lines), "actions": acts[:4], "entity_id": entity_id}
