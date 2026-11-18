"""Analyse de la configuration : santé système, optimisation, nettoyage."""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from ..const import CAT_CONFIG, CAT_REPAIRS, CAT_SYSTEM, CRITICAL, INFO, WARNING
from ..models import action, issue

if TYPE_CHECKING:
    from ..core import Majordome

_LOGGER = logging.getLogger(__name__)

DB_FILE = "home-assistant_v2.db"
CHATTY_PER_DAY = 3000
_CHATTY_CACHE: dict[str, Any] = {"ts": 0.0, "rows": []}


def _supervisor_client(hass: HomeAssistant):
    """Client Supervisor si on est sur HA OS / Supervised."""
    try:
        from homeassistant.components.hassio import is_hassio  # noqa: PLC0415

        if not is_hassio(hass):
            return None
        from homeassistant.components.hassio.handler import get_supervisor_client  # noqa: PLC0415

        return get_supervisor_client(hass)
    except Exception:  # noqa: BLE001
        return None


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return addr.is_private or addr.is_link_local or addr.is_loopback


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


async def _supervisor_checks(hass: HomeAssistant, issues: list[dict[str, Any]], system: dict[str, Any]) -> None:
    client = _supervisor_client(hass)
    if client is None:
        system["supervisor"] = False
        return
    system["supervisor"] = True

    # Sauvegardes
    try:
        backups = await client.backups.list()
        dates = [d for d in (_as_dt(getattr(b, "date", None)) for b in backups) if d]
        last = max(dates) if dates else None
        system["last_backup"] = last.isoformat() if last else None
        age_days = (dt_util.utcnow() - last).days if last else None
        if last is None:
            issues.append(
                issue(
                    "backup_none",
                    CAT_SYSTEM,
                    CRITICAL,
                    "Aucune sauvegarde",
                    fix="Active les sauvegardes automatiques (Paramètres > Système > Sauvegardes), idéalement vers un NAS ou le cloud.",
                    actions=[action("backup_now", "Faire une sauvegarde maintenant", confirm="Lancer une sauvegarde complète maintenant ?")],
                )
            )
        elif age_days is not None and age_days > 7:
            issues.append(
                issue(
                    "backup_old",
                    CAT_SYSTEM,
                    WARNING,
                    f"Dernière sauvegarde il y a {age_days} jours",
                    fix="Active les sauvegardes automatiques quotidiennes.",
                    actions=[action("backup_now", "Faire une sauvegarde maintenant", confirm="Lancer une sauvegarde complète maintenant ?")],
                )
            )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Liste des sauvegardes indisponible : %s", err)

    # Disque
    try:
        host = await client.host.info()
        free = getattr(host, "disk_free", None)
        total = getattr(host, "disk_total", None)
        system["disk_free_gb"] = free
        system["disk_total_gb"] = total
        if free is not None and total:
            pct = free / total * 100
            if free < 2 or pct < 5:
                sev = CRITICAL
            elif free < 5 or pct < 15:
                sev = WARNING
            else:
                sev = None
            if sev:
                issues.append(
                    issue(
                        "disk_low",
                        CAT_SYSTEM,
                        sev,
                        f"Plus que {free:.1f} Go libres sur le disque",
                        fix="Supprime les vieilles sauvegardes locales, les fichiers de base corrompus et réduis l'historique.",
                    )
                )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Infos hôte indisponibles : %s", err)

    # Centre de résolution du Supervisor
    try:
        res = await client.resolution.info()
        for reason in getattr(res, "unhealthy", []) or []:
            issues.append(
                issue(
                    f"sup_unhealthy_{reason}",
                    CAT_SYSTEM,
                    CRITICAL,
                    f"Système jugé « non sain » par le Supervisor : {reason}",
                    fix="Ouvre Paramètres > Système > Réparations pour le détail.",
                    link="/config/repairs",
                )
            )
        for reason in getattr(res, "unsupported", []) or []:
            issues.append(
                issue(
                    f"sup_unsupported_{reason}",
                    CAT_SYSTEM,
                    WARNING,
                    f"Installation « non supportée » : {reason}",
                    fix="Les mises à jour peuvent être bloquées tant que ce point n'est pas réglé.",
                    link="/config/repairs",
                )
            )
        for sup_issue in getattr(res, "issues", []) or []:
            kind = getattr(sup_issue, "type", "")
            context = getattr(sup_issue, "context", "")
            ref = getattr(sup_issue, "reference", None)
            issues.append(
                issue(
                    f"sup_issue_{kind}_{ref}",
                    CAT_SYSTEM,
                    WARNING,
                    f"Supervisor : {kind} ({context}{' – ' + str(ref) if ref else ''})",
                    fix="Détail et correctif proposés dans Paramètres > Réparations.",
                    link="/config/repairs",
                )
            )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Centre de résolution indisponible : %s", err)


def _repairs(hass: HomeAssistant, issues: list[dict[str, Any]]) -> None:
    reg = ir.async_get(hass)
    for (domain, issue_id), item in reg.issues.items():
        if not item.active or item.dismissed_version is not None:
            continue
        sev = {"critical": CRITICAL, "error": CRITICAL, "warning": WARNING}.get(str(item.severity), INFO)
        issues.append(
            issue(
                f"repair_{domain}_{issue_id}",
                CAT_REPAIRS,
                sev,
                f"Réparation proposée par « {domain} » : {issue_id.replace('_', ' ')}",
                fix="Home Assistant propose un correctif guidé.",
                link=item.learn_more_url or "/config/repairs",
            )
        )


def _config_entries(hass: HomeAssistant, issues: list[dict[str, Any]]) -> None:
    bad_states = {
        ConfigEntryState.SETUP_ERROR: "erreur au démarrage",
        ConfigEntryState.SETUP_RETRY: "réessaie en boucle",
        ConfigEntryState.MIGRATION_ERROR: "erreur de migration",
        ConfigEntryState.FAILED_UNLOAD: "déchargement impossible",
    }
    for entry in hass.config_entries.async_entries():
        if entry.disabled_by or entry.domain == "zha":
            continue  # ZHA est traité par l'analyse Zigbee
        reason = bad_states.get(entry.state)
        if reason is None and entry.state is ConfigEntryState.NOT_LOADED:
            reason = "non chargée"
        if reason is None:
            continue
        issues.append(
            issue(
                f"entry_{entry.entry_id}",
                CAT_CONFIG,
                WARNING,
                f"Intégration « {entry.title} » ({entry.domain}) : {reason}",
                detail=str(entry.reason or ""),
                fix="Essaie de la recharger. Si tu ne l'utilises plus, désactive-la : elle ralentit le démarrage.",
                actions=[
                    action("reload_entry", "Recharger", {"entry_id": entry.entry_id}),
                    action(
                        "disable_entry",
                        "Désactiver",
                        {"entry_id": entry.entry_id},
                        confirm=f"Désactiver l'intégration « {entry.title} » ? (réactivable)",
                    ),
                ],
            )
        )


def _entities(hass: HomeAssistant, issues: list[dict[str, Any]]) -> dict[str, Any]:
    ent_reg = er.async_get(hass)
    now = dt_util.utcnow()
    per_platform: dict[str, list[str]] = {}
    total_platform: dict[str, int] = {}
    orphans: list[str] = []

    for entry in ent_reg.entities.values():
        if entry.disabled_by:
            continue
        total_platform[entry.platform] = total_platform.get(entry.platform, 0) + 1
        state = hass.states.get(entry.entity_id)
        if state is None:
            continue
        if state.attributes.get("restored"):
            orphans.append(entry.entity_id)
            continue
        if state.state == "unavailable" and (now - state.last_changed).total_seconds() > 24 * 3600:
            per_platform.setdefault(entry.platform, []).append(entry.entity_id)

    for platform, ents in sorted(per_platform.items(), key=lambda kv: -len(kv[1])):
        if platform in ("mobile_app",):
            continue  # téléphone hors réseau : normal
        ratio = len(ents) / max(1, total_platform.get(platform, 1))
        sev = WARNING if ratio > 0.5 and len(ents) >= 5 else INFO
        issues.append(
            issue(
                f"unavailable_{platform}",
                CAT_CONFIG,
                sev,
                f"{len(ents)} entité(s) « {platform} » indisponibles depuis plus de 24 h",
                detail=", ".join(ents[:12]) + (" …" if len(ents) > 12 else ""),
                fix=(
                    "Si toute l'intégration est concernée, son appareil ou son service ne répond plus. "
                    "Si ce sont des entités inutiles, désactive-les pour alléger Home Assistant."
                ),
                entities=ents,
            )
        )

    if orphans:
        issues.append(
            issue(
                "orphans",
                CAT_CONFIG,
                INFO,
                f"{len(orphans)} entité(s) fantôme(s)",
                detail="Ces entités ne sont plus fournies par aucune intégration : " + ", ".join(orphans[:12]),
                fix="On peut les supprimer sans risque.",
                actions=[
                    action(
                        "remove_entities",
                        "Supprimer les entités fantômes",
                        {"entity_ids": orphans},
                        confirm=f"Supprimer {len(orphans)} entité(s) fantôme(s) du registre ?",
                    )
                ],
                entities=orphans,
            )
        )
    return {"orphans": len(orphans)}


def _automations(hass: HomeAssistant, issues: list[dict[str, Any]]) -> None:
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    component = (hass.data.get("entity_components") or {}).get("automation")
    entities = list(component.entities) if component else []
    for auto in entities:
        state = hass.states.get(auto.entity_id)
        name = state.attributes.get("friendly_name", auto.entity_id) if state else auto.entity_id
        auto_id = state.attributes.get("id") if state else None
        link = f"/config/automation/edit/{auto_id}" if auto_id else "/config/automation/dashboard"
        if state and state.state == "unavailable":
            issues.append(
                issue(
                    f"auto_broken_{auto.entity_id}",
                    CAT_CONFIG,
                    WARNING,
                    f"Automatisation « {name} » hors service",
                    fix="Elle n'a pas pu se charger (souvent un appareil supprimé). Ouvre-la pour la corriger.",
                    link=link,
                )
            )
            continue
        missing: list[str] = []
        try:
            for ent in getattr(auto, "referenced_entities", set()) or set():
                if hass.states.get(ent) is None and ent_reg.async_get(ent) is None:
                    missing.append(ent)
            for dev_id in getattr(auto, "referenced_devices", set()) or set():
                if dev_reg.async_get(dev_id) is None:
                    missing.append(f"appareil {dev_id[:8]}…")
        except Exception:  # noqa: BLE001
            continue
        if missing:
            issues.append(
                issue(
                    f"auto_missing_{auto.entity_id}",
                    CAT_CONFIG,
                    WARNING,
                    f"Automatisation « {name} » : référence(s) introuvable(s)",
                    detail=", ".join(missing[:8]),
                    fix="Remplace ces entités/appareils par ceux qui existent aujourd'hui.",
                    link=link,
                )
            )


def _db_checks(hass: HomeAssistant, issues: list[dict[str, Any]], system: dict[str, Any]) -> None:
    cfg = hass.config.config_dir
    try:
        names = os.listdir(cfg)
    except OSError:
        return
    corrupt = [n for n in names if n.startswith(DB_FILE + ".corrupt")]
    if corrupt:
        size = sum(os.path.getsize(os.path.join(cfg, n)) for n in corrupt) / 1e6
        issues.append(
            issue(
                "db_corrupt_files",
                CAT_CONFIG,
                INFO,
                f"{len(corrupt)} ancienne(s) base(s) corrompue(s) ({size:.0f} Mo)",
                detail=", ".join(corrupt),
                fix="Ce sont des copies de bases abîmées, gardées au cas où. Elles prennent de la place et sont aussi incluses dans les sauvegardes.",
                actions=[
                    action(
                        "delete_corrupt_db",
                        "Supprimer ces fichiers",
                        {"files": corrupt},
                        confirm=f"Supprimer définitivement {len(corrupt)} fichier(s) de base corrompue ({size:.0f} Mo) ?",
                    )
                ],
            )
        )
    bans = os.path.join(cfg, "ip_bans.yaml")
    if os.path.exists(bans):
        try:
            with open(bans, encoding="utf-8") as fh:
                ips = [ln.rstrip(":\n ").strip("'\"") for ln in fh if ln and not ln[0].isspace() and ln.strip().endswith(":")]
        except OSError:
            ips = []
        local = [ip for ip in ips if _is_private(ip)]
        system["banned_ips"] = len(ips)
        if local:
            issues.append(
                issue(
                    "ban_local",
                    CAT_SYSTEM,
                    CRITICAL,
                    f"{len(local)} appareil(s) de ta maison banni(s)",
                    detail="Adresses locales bannies : " + ", ".join(local) + ". Ces appareils ne peuvent plus ouvrir Home Assistant (erreur 403).",
                    fix="Souvent dû à une session expirée qui retente sa connexion en boucle. Débannis-les puis redémarre Home Assistant.",
                    actions=[
                        action(
                            "unban_local",
                            "Débannir et redémarrer",
                            {"ips": local},
                            confirm="Retirer les adresses locales de la liste des bannis et redémarrer Home Assistant ?",
                        )
                    ],
                )
            )
    db = os.path.join(cfg, DB_FILE)
    if os.path.exists(db):
        size_mb = os.path.getsize(db) / 1e6
        system["db_size_mb"] = round(size_mb)
        if size_mb > 1500:
            issues.append(
                issue(
                    "db_big",
                    CAT_CONFIG,
                    WARNING if size_mb < 4000 else CRITICAL,
                    f"Base de l'historique volumineuse ({size_mb / 1000:.1f} Go)",
                    fix="Réduis purge_keep_days (ex. 7) et exclue les capteurs les plus bavards (voir liste dans Optimisation).",
                )
            )


async def _chatty_entities(hass: HomeAssistant, system: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    """Entités qui écrivent le plus dans l'historique (sur 24 h)."""
    if time.time() - _CHATTY_CACHE["ts"] > 6 * 3600:
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.util import session_scope  # noqa: PLC0415
            from sqlalchemy import text  # noqa: PLC0415

            since = time.time() - 86400

            def _query() -> list[tuple[str, int]]:
                with session_scope(hass=hass, read_only=True) as session:
                    rows = session.execute(
                        text(
                            "SELECT sm.entity_id, COUNT(*) AS c FROM states s "
                            "JOIN states_meta sm ON s.metadata_id = sm.metadata_id "
                            "WHERE s.last_updated_ts > :since "
                            "GROUP BY sm.entity_id ORDER BY c DESC LIMIT 12"
                        ),
                        {"since": since},
                    ).fetchall()
                return [(r[0], int(r[1])) for r in rows]

            rows = await get_instance(hass).async_add_executor_job(_query)
            _CHATTY_CACHE.update(ts=time.time(), rows=rows)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Requête des entités bavardes impossible : %s", err)
            _CHATTY_CACHE.update(ts=time.time(), rows=[])
    rows = _CHATTY_CACHE["rows"]
    system["chatty"] = [{"entity_id": e, "per_day": c} for e, c in rows]
    heavy = [(e, c) for e, c in rows if c >= CHATTY_PER_DAY]
    if heavy:
        yaml_lines = "\n".join(f"      - {e}" for e, _ in heavy)
        issues.append(
            issue(
                "chatty",
                CAT_CONFIG,
                INFO,
                f"{len(heavy)} capteur(s) très bavard(s) remplissent l'historique",
                detail=", ".join(f"{e} ({c}/jour)" for e, c in heavy),
                fix=(
                    "Si tu n'as pas besoin de leur historique, exclue-les dans configuration.yaml :\n"
                    "recorder:\n  exclude:\n    entities:\n" + yaml_lines
                ),
            )
        )


async def async_analyze(hass: HomeAssistant, md: "Majordome") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Retourne (infos système, problèmes)."""
    issues: list[dict[str, Any]] = []
    system: dict[str, Any] = {}
    await _supervisor_checks(hass, issues, system)
    _repairs(hass, issues)
    _config_entries(hass, issues)
    system.update(_entities(hass, issues))
    _automations(hass, issues)
    await hass.async_add_executor_job(_db_checks, hass, issues, system)
    await _chatty_entities(hass, system, issues)
    return system, issues
