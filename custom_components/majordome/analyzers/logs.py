"""Analyse du journal : regroupe les erreurs par cause et les explique."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from ..const import CAT_LOGS, CRITICAL, INFO, WARNING
from ..models import action, integration_from_logger, issue


@dataclass(frozen=True)
class Rule:
    """Une règle de la base de connaissances."""

    rule_id: str
    pattern: re.Pattern
    severity: str
    title: str
    explain: str
    fix: str
    logger: str | None = None  # filtre optionnel sur le nom du logger


def _r(rule_id, regex, severity, title, explain, fix, logger=None) -> Rule:
    return Rule(rule_id, re.compile(regex, re.I | re.S), severity, title, explain, fix, logger)


RULES: list[Rule] = [
    _r(
        "dns",
        r"Timeout while contacting DNS|Temporary failure in name resolution|"
        r"Name or service not known|Could not contact DNS servers|DNS lookup failed",
        WARNING,
        "Problème de résolution DNS",
        "Home Assistant n'arrive pas à traduire des noms de domaine en adresses. "
        "Toutes les intégrations cloud tombent en même temps.",
        "Configure des serveurs DNS fiables (1.1.1.1 / 9.9.9.9) pour Home Assistant, "
        "et vérifie que ta box ne sature pas.",
    ),
    _r(
        "connect",
        r"Cannot connect to host|ClientConnectorError|Connection refused|"
        r"ConnectTimeout|Timeout fetching|TimeoutError|timed out",
        INFO,
        "Service injoignable",
        "Une intégration n'arrive pas à joindre son appareil ou son serveur cloud.",
        "Vérifie que l'appareil est allumé et sur le réseau. Si c'est un service cloud, "
        "c'est souvent passager.",
    ),
    _r(
        "cloud_busy",
        r"servers are busy|rate limit|too many requests|HTTP 429|status 429",
        INFO,
        "Serveur cloud saturé",
        "Le serveur du fabricant limite ou refuse les requêtes.",
        "Rien à faire si c'est ponctuel. Si c'est permanent, augmente l'intervalle "
        "d'interrogation de l'intégration.",
    ),
    _r(
        "automation_broken",
        r"failed to setup (?:actions|triggers|conditions).*?(?:Unknown device|Unknown entity|"
        r"not a valid|Entity .* is neither)",
        WARNING,
        "Automatisation cassée",
        "Une automatisation fait référence à un appareil ou une entité qui n'existe plus. "
        "Elle a été désactivée par Home Assistant.",
        "Ouvre l'automatisation et remplace l'appareil supprimé par le nouveau.",
    ),
    _r(
        "blocking_call",
        r"Detected blocking call|Detected that custom integration|"
        r"Detected code that|calls .* from a thread",
        INFO,
        "Intégration perso mal codée",
        "Une intégration personnalisée utilise du code obsolète qui ralentit Home Assistant "
        "et cassera dans une prochaine version.",
        "Mets à jour cette intégration via HACS ; sinon signale-le à son auteur.",
    ),
    _r(
        "db_corrupt",
        r"database disk image is malformed|database is corrupt|"
        r"The system could not validate that the sqlite3 database",
        CRITICAL,
        "Base de données corrompue",
        "La base de l'historique a été corrompue (souvent coupure de courant ou carte SD usée).",
        "Home Assistant en recrée une automatiquement. Pense à un onduleur et à un SSD au lieu "
        "d'une carte SD.",
    ),
    _r(
        "db_locked",
        r"database is locked|Error executing query|Error saving events|"
        r"Ended unfinished session",
        WARNING,
        "Base de données surchargée",
        "L'enregistreur n'arrive pas à suivre : trop d'écritures ou disque trop lent.",
        "Exclure de l'historique les capteurs très bavards et réduire purge_keep_days.",
    ),
    _r(
        "zigbee_delivery",
        r"MAC_NO_ACK|Delivery failed|Failed to deliver|NWK_NO_ROUTE|MAC_CHANNEL_ACCESS_FAILURE|"
        r"No network route|Failed to send|ZCL command .* failed",
        WARNING,
        "Messages Zigbee non délivrés",
        "Le coordinateur n'arrive pas à joindre certains appareils : maillage trop faible, "
        "interférences Wi-Fi/USB ou appareil débranché.",
        "Éloigne le dongle du Pi avec une rallonge USB, ajoute des appareils secteur (routeurs) "
        "près des appareils isolés, évite les canaux Wi-Fi qui se chevauchent.",
    ),
    _r(
        "zigbee_coordinator",
        r"NCP entered failed state|Coordinator.*(?:failed|offline|disconnected)|"
        r"Failed to start Zigbee|adapter.*(?:failed|disconnected)|serial port.*(?:closed|error)",
        CRITICAL,
        "Le dongle Zigbee a décroché",
        "Le coordinateur Zigbee a perdu la connexion : plus aucune commande Zigbee ne passe.",
        "Redémarre Zigbee2MQTT. Si ça se répète : rallonge USB, alimentation du Pi, "
        "firmware du dongle à jour.",
    ),
    _r(
        "mqtt_disconnect",
        r"Disconnected from MQTT|Unexpected disconnect|MQTT.*(?:connection|connect).*(?:lost|failed|refused)",
        WARNING,
        "Déconnexions MQTT",
        "Home Assistant perd la connexion au broker MQTT (Mosquitto) : Zigbee2MQTT ne répond plus pendant ce temps.",
        "Vérifie que le module Mosquitto tourne et que le Pi n'est pas à court de mémoire.",
    ),
    _r(
        "proxy",
        r"X-Forwarded-For|untrusted proxy|reverse proxy",
        INFO,
        "Requêtes proxy invalides",
        "Des requêtes arrivent avec des en-têtes falsifiés (souvent des robots).",
        "Elles sont rejetées. Les règles Cloudflare réduisent ce bruit.",
    ),
    _r(
        "security",
        r"Filtered a (?:potential harmful|request with)|Login attempt or request with invalid authentication",
        INFO,
        "Tentatives d'accès bloquées",
        "Des robots tentent d'accéder à Home Assistant depuis Internet. Elles sont bloquées.",
        "Normal si ton accès distant est ouvert. Le bannissement automatique est le bon réflexe.",
    ),
    _r(
        "template",
        r"Template variable (?:warning|error)|UndefinedError|TemplateError|"
        r"has no attribute|unknown is not a number|could not convert",
        INFO,
        "Modèle (template) fragile",
        "Un modèle Jinja utilise une valeur absente ou non numérique.",
        "Ajoute un filtre par défaut, ex. {{ states('sensor.x') | float(0) }}.",
    ),
    _r(
        "setup_failed",
        r"Error setting up entry|Setup failed for|Config entry .* not ready|"
        r"Error while setting up .* platform|Unable to prepare setup",
        WARNING,
        "Intégration qui ne démarre pas",
        "Une intégration n'a pas pu se lancer au démarrage.",
        "Recharge-la depuis Paramètres > Appareils et services ; si l'erreur persiste, "
        "vérifie l'appareil ou les identifiants.",
    ),
    _r(
        "slow_setup",
        r"Setup of .* is taking over|Waiting on integrations to complete setup|"
        r"Something is blocking Home Assistant",
        INFO,
        "Démarrage lent",
        "Une intégration met trop de temps à démarrer et retarde tout Home Assistant.",
        "Vérifie que son appareil est joignable ; désactive-la si tu ne l'utilises plus.",
    ),
    _r(
        "memory",
        r"MemoryError|Cannot allocate memory|out of memory",
        CRITICAL,
        "Mémoire saturée",
        "Le Raspberry Pi manque de mémoire.",
        "Arrête les modules complémentaires inutiles (ex. Frigate si peu utilisé).",
    ),
    _r(
        "fetch_generic",
        r"Error fetching .* data|Error requesting .* data|Update of .* failed|Error updating",
        INFO,
        "Mise à jour de données échouée",
        "Une intégration n'a pas pu rafraîchir ses données.",
        "Souvent temporaire. Si ça dure, vérifie l'appareil ou le service.",
    ),
]

SEVERITY_RANK = {CRITICAL: 0, WARNING: 1, INFO: 2}


def get_records(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Récupère les entrées du journal système (warnings/erreurs)."""
    handler = hass.data.get("system_log")
    if handler is None:
        return []
    try:
        return list(handler.records.to_list())
    except Exception:  # noqa: BLE001
        return []


def _text(record: dict[str, Any]) -> str:
    msg = record.get("message")
    if isinstance(msg, list):
        msg = "\n".join(str(m) for m in msg)
    exc = record.get("exception") or ""
    return f"{msg}\n{exc}"


def classify(record: dict[str, Any]) -> Rule | None:
    """Trouve la règle qui correspond à une entrée du journal."""
    text = _text(record)
    for rule in RULES:
        if rule.logger and not str(record.get("name", "")).startswith(rule.logger):
            continue
        if rule.pattern.search(text):
            return rule
    return None


def analyze(hass: HomeAssistant) -> tuple[list[dict], list[dict]]:
    """Retourne (groupes d'erreurs, problèmes)."""
    groups: dict[str, dict[str, Any]] = {}
    for rec in get_records(hass):
        level = str(rec.get("level", "")).upper()
        if level not in ("WARNING", "ERROR", "CRITICAL"):
            continue
        rule = classify(rec)
        integ = integration_from_logger(str(rec.get("name", ""))) or str(rec.get("name", "?"))
        key = rule.rule_id if rule else f"other:{integ}"
        grp = groups.setdefault(
            key,
            {
                "id": key,
                "title": rule.title if rule else f"Erreurs de « {integ} »",
                "explain": rule.explain if rule else "Erreur non répertoriée dans la base de Majordome.",
                "fix": rule.fix if rule else "Consulte le message complet ci-dessous.",
                "severity": rule.severity if rule else (WARNING if level != "WARNING" else INFO),
                "count": 0,
                "integrations": set(),
                "samples": [],
                "last": 0,
            },
        )
        grp["count"] += int(rec.get("count", 1) or 1)
        grp["integrations"].add(integ)
        grp["last"] = max(grp["last"], float(rec.get("timestamp", 0) or 0))
        msg = rec.get("message")
        first = msg[0] if isinstance(msg, list) and msg else str(msg)
        if len(grp["samples"]) < 4:
            grp["samples"].append(f"[{integ}] {str(first)[:300]}")

    result: list[dict[str, Any]] = []
    for grp in groups.values():
        grp["integrations"] = sorted(grp["integrations"])
        result.append(grp)

    # Escalade : DNS ou injoignable sur plusieurs intégrations = panne réseau générale
    net = [g for g in result if g["id"] in ("dns", "connect")]
    net_integrations = sorted({i for g in net for i in g["integrations"]})
    for g in result:
        if g["id"] == "dns" and len(g["integrations"]) >= 2:
            g["severity"] = CRITICAL

    result.sort(key=lambda g: (SEVERITY_RANK.get(g["severity"], 9), -g["count"]))

    issues: list[dict[str, Any]] = []
    for g in result:
        if g["id"] in ("security", "proxy"):
            continue  # traité par la section sécurité
        acts = []
        if g["id"] == "dns":
            acts.append(
                action(
                    "set_dns",
                    "Utiliser les DNS 1.1.1.1 et 9.9.9.9",
                    {"servers": ["dns://1.1.1.1", "dns://9.9.9.9"]},
                    confirm="Majordome va régler le DNS de secours du Supervisor sur Cloudflare (1.1.1.1) et Quad9 (9.9.9.9). Continuer ?",
                )
            )
        if g["id"] == "automation_broken":
            acts.append(action("open", "Ouvrir les automatisations", {"path": "/config/automation/dashboard"}))
        if g["id"] in ("zigbee_coordinator",):
            acts.append(action("z2m_restart", "Redémarrer Zigbee2MQTT", confirm="Redémarrer Zigbee2MQTT ?"))
        if g["severity"] == INFO and g["count"] < 20:
            continue  # bruit de fond : visible dans l'onglet Erreurs uniquement
        issues.append(
            issue(
                f"log_{g['id']}",
                CAT_LOGS,
                g["severity"],
                f"{g['title']} ({g['count']}×)",
                detail=g["explain"] + " Concerné : " + ", ".join(g["integrations"][:8]),
                fix=g["fix"],
                actions=acts,
            )
        )

    if len(net_integrations) >= 3 and not any(i["id"] == "log_dns" for i in issues):
        issues.append(
            issue(
                "log_network_general",
                CAT_LOGS,
                WARNING,
                f"{len(net_integrations)} intégrations ont des soucis réseau en même temps",
                detail="Concerné : " + ", ".join(net_integrations[:10]),
                fix="Une cause commune est probable (DNS, box, Wi-Fi). Vérifie la connexion du Pi.",
                actions=[
                    action(
                        "set_dns",
                        "Utiliser les DNS 1.1.1.1 et 9.9.9.9",
                        {"servers": ["dns://1.1.1.1", "dns://9.9.9.9"]},
                        confirm="Régler le DNS de secours du Supervisor sur 1.1.1.1 et 9.9.9.9 ?",
                    )
                ],
            )
        )

    # Sécurité : résumé
    sec = [g for g in result if g["id"] in ("security", "proxy")]
    total = sum(g["count"] for g in sec)
    if total:
        issues.append(
            issue(
                "log_security",
                CAT_LOGS,
                INFO,
                f"{total} tentatives d'accès bloquées",
                detail="Des robots scannent ton accès distant. Home Assistant les a refusées.",
                fix="Vérifie que les règles Cloudflare (Europe uniquement) sont actives.",
            )
        )
    return result, issues
