"""Analyse des mises à jour : risque, compatibilité, changements cassants."""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integrations

from ..const import (
    CAT_UPDATES,
    CRITICAL,
    INFO,
    RISK_BLOCKED,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    WARNING,
)
from ..models import action, issue, parse_version, version_jump

if TYPE_CHECKING:
    from ..core import Majordome

_LOGGER = logging.getLogger(__name__)

FEATURE_INSTALL = 1
FEATURE_SPECIFIC_VERSION = 2
FEATURE_BACKUP = 8
FEATURE_RELEASE_NOTES = 16

BREAKING_WORDS = (
    "breaking",
    "incompatible",
    "migration",
    "migrate",
    "removed",
    "no longer",
    "deprecat",
    "renamed",
    "must re-pair",
    "re-pair",
    "rename",
    "cassant",
    "supprim",
)

_REQ_HA_RE = re.compile(
    r"(?:home\s*assistant|\bha\b)[^0-9\n]{0,40}?(20\d\d\.\d{1,2}(?:\.\d+)?)",
    re.IGNORECASE,
)

BLOG_INDEX = "https://www.home-assistant.io/blog/categories/core/"
BLOG_BASE = "https://www.home-assistant.io"
_RELEASE_LINK_RE = re.compile(r'href="(/blog/\d{4}/\d{2}/\d{2}/release-(\d{4})(\d{1,2})/)"')
_SUMMARY_RE = re.compile(r"<summary[^>]*>(.*?)</summary>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# Cache simple en mémoire
_NOTES_CACHE: dict[str, tuple[float, str]] = {}
_BLOG_CACHE: dict[str, tuple[float, list[str]]] = {}
CACHE_TTL = 6 * 3600


def get_update_entity(hass: HomeAssistant, entity_id: str):
    """Retourne l'objet entité update (pour lire les notes de version)."""
    components = hass.data.get("entity_components") or {}
    component = components.get("update")
    if component is None:
        return None
    try:
        return component.get_entity(entity_id)
    except Exception:  # noqa: BLE001
        return None


async def async_release_notes(hass: HomeAssistant, entity_id: str) -> str:
    """Notes de version d'une entité update (avec cache)."""
    state = hass.states.get(entity_id)
    latest = state.attributes.get("latest_version") if state else None
    key = f"{entity_id}:{latest}"
    cached = _NOTES_CACHE.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]
    notes = ""
    entity = get_update_entity(hass, entity_id)
    if entity is not None and (entity.supported_features or 0) & FEATURE_RELEASE_NOTES:
        try:
            notes = await entity.async_release_notes() or ""
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Notes de version indisponibles pour %s: %s", entity_id, err)
    if not notes and state:
        notes = state.attributes.get("release_summary") or ""
    _NOTES_CACHE[key] = (time.time(), notes)
    return notes


def _cal_months(version: tuple[int, ...] | None) -> int | None:
    if not version or len(version) < 2:
        return None
    return version[0] * 12 + version[1]


async def _core_release_posts(hass: HomeAssistant) -> dict[str, str]:
    """{'2026.11': url, ...} depuis le blog officiel."""
    cached = _BLOG_CACHE.get("index")
    if cached and time.time() - cached[0] < CACHE_TTL:
        links = cached[1]
    else:
        session = async_get_clientsession(hass)
        async with session.get(BLOG_INDEX, timeout=20) as resp:
            html = await resp.text()
        links = [m.group(0) for m in _RELEASE_LINK_RE.finditer(html)]
        _BLOG_CACHE["index"] = (time.time(), links)
    result: dict[str, str] = {}
    for raw in links:
        m = _RELEASE_LINK_RE.match(raw)
        if m:
            result[f"{int(m.group(2))}.{int(m.group(3))}"] = BLOG_BASE + m.group(1)
    return result


async def _breaking_sections(hass: HomeAssistant, url: str) -> list[str]:
    """Liste des intégrations citées dans 'Backward-incompatible changes'."""
    cached = _BLOG_CACHE.get(url)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]
    session = async_get_clientsession(hass)
    async with session.get(url, timeout=20) as resp:
        html = await resp.text()
    # L'ancre est en id= ou name= selon les versions du site ; à défaut, le titre <h2>.
    start = html.find('id="backward-incompatible-changes"')
    if start == -1:
        start = html.find('name="backward-incompatible-changes"')
    if start == -1:
        m = re.search(r"<h2[^>]*>\s*Backward-incompatible changes", html)
        start = m.end() if m else -1
    names: list[str] = []
    if start != -1:
        end = html.find("<h2", start + 10)
        section = html[start : end if end != -1 else None]
        for raw in _SUMMARY_RE.findall(section):
            text = _TAG_RE.sub("", raw).strip()
            if text:
                names.append(text)
    _BLOG_CACHE[url] = (time.time(), names)
    return names


async def _user_integrations(hass: HomeAssistant) -> dict[str, str]:
    """{nom en minuscules: domaine} pour les intégrations configurées."""
    domains = {e.domain for e in hass.config_entries.async_entries()}
    domains |= {c for c in hass.config.components if "." not in c}
    found = await async_get_integrations(hass, domains)
    names: dict[str, str] = {}
    for domain, integ in found.items():
        if isinstance(integ, Exception):
            continue
        names[integ.name.lower()] = domain
        names[domain.lower()] = domain
    return names


async def _core_breaking_for_user(
    hass: HomeAssistant, installed: str, latest: str
) -> tuple[list[str], list[str]]:
    """Retourne (intégrations de l'utilisateur touchées, versions analysées)."""
    a = _cal_months(parse_version(installed))
    b = _cal_months(parse_version(latest))
    if a is None or b is None or b <= a:
        return [], []
    try:
        posts = await _core_release_posts(hass)
        mine = await _user_integrations(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Blog HA inaccessible: %s", err)
        return [], []
    hits: set[str] = set()
    checked: list[str] = []
    patterns = [
        (re.compile(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])"), n)
        for n in mine
        if len(n) >= 3
    ]
    for months in range(a + 1, b + 1):
        year, month = divmod(months, 12)
        if month == 0:
            year, month = year - 1, 12
        ver = f"{year}.{month}"
        url = posts.get(ver)
        if not url:
            continue
        try:
            names = await _breaking_sections(hass, url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Lecture %s impossible: %s", url, err)
            continue
        if not names:
            # Page non comprise : on ne prétend pas qu'il n'y a rien
            continue
        checked.append(ver)
        for name in names:
            low = name.lower()
            if low in mine or any(rx.search(low) for rx, _ in patterns):
                hits.add(f"{name} ({ver})")
    return sorted(hits), checked


def _source(hass: HomeAssistant, entity_id: str) -> str:
    entry = er.async_get(hass).async_get(entity_id)
    platform = entry.platform if entry else ""
    if platform == "hassio":
        if entity_id.endswith(("home_assistant_core_update", "core_update")):
            return "core"
        if "operating_system" in entity_id or entity_id.endswith("os_update"):
            return "os"
        if "supervisor" in entity_id:
            return "supervisor"
        return "addon"
    if platform == "hacs":
        return "hacs"
    if platform == "mqtt":
        return "zigbee_firmware"
    return platform or "other"


SOURCE_LABEL = {
    "core": "Home Assistant Core",
    "os": "Système (OS)",
    "supervisor": "Supervisor",
    "addon": "Module complémentaire",
    "hacs": "HACS",
    "zigbee_firmware": "Firmware Zigbee",
}


def _bump(risk: str) -> str:
    return {RISK_LOW: RISK_MEDIUM, RISK_MEDIUM: RISK_HIGH}.get(risk, risk)


async def async_analyze(hass: HomeAssistant, md: "Majordome") -> tuple[list[dict], list[dict]]:
    """Retourne (liste des mises à jour analysées, problèmes)."""
    updates: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    ha_now = parse_version(HA_VERSION)

    for state in hass.states.async_all("update"):
        if state.state != "on":
            continue
        attrs = state.attributes
        installed = attrs.get("installed_version")
        latest = attrs.get("latest_version")
        title = attrs.get("title") or attrs.get("friendly_name") or state.entity_id
        src = _source(hass, state.entity_id)
        jump = version_jump(installed, latest)
        features = attrs.get("supported_features") or 0
        reasons: list[str] = []

        if src == "core":
            months = None
            a, b = _cal_months(parse_version(installed)), _cal_months(parse_version(latest))
            if a is not None and b is not None:
                months = b - a
            risk = RISK_LOW if months == 0 else RISK_MEDIUM
            hits, checked = await _core_breaking_for_user(hass, installed, latest)
            if hits:
                risk = RISK_HIGH
                reasons.append(
                    "Changements cassants qui concernent TES intégrations : " + ", ".join(hits)
                )
            elif checked:
                reasons.append(
                    "Aucun changement cassant détecté pour tes intégrations dans "
                    + ", ".join(checked)
                )
            elif months:
                reasons.append(
                    "Impossible de lire les changements cassants sur le blog officiel : lis-les avant de mettre à jour."
                )
            if months and months >= 3:
                reasons.append(f"Tu as {months} versions mensuelles de retard.")
            latest_v = parse_version(latest) or ()
            if len(latest_v) >= 3 and latest_v[2] == 0:
                reasons.append("Version .0 toute fraîche : attendre le correctif .1 est plus prudent.")
                risk = _bump(risk) if risk == RISK_LOW else risk
        else:
            risk = {"major": RISK_HIGH, "minor": RISK_MEDIUM, "patch": RISK_LOW}.get(jump, RISK_MEDIUM)
            if jump == "major":
                reasons.append("Changement de version majeure : risque de changements cassants.")
            notes = await async_release_notes(hass, state.entity_id)
            low = notes.lower()
            found = sorted({w for w in BREAKING_WORDS if w in low})
            if found:
                if risk != RISK_HIGH:
                    risk = _bump(risk)
                reasons.append("Les notes de version mentionnent : " + ", ".join(found))
            for m in _REQ_HA_RE.finditer(notes):
                req = parse_version(m.group(1))
                if req and ha_now and req > ha_now and "require" in low[max(0, m.start() - 60) : m.end()].lower():
                    risk = RISK_BLOCKED
                    reasons.append(f"Nécessite Home Assistant {m.group(1)} (tu as {HA_VERSION}).")
                    break
            if src == "addon" and "zigbee2mqtt" in str(title).lower() and jump in ("major", "minor"):
                risk = RISK_HIGH if jump == "major" else _bump(risk)
                reasons.append("Zigbee2MQTT : vérifie la compatibilité de ton dongle et de tes appareils.")

        recommendation = {
            RISK_LOW: "Correctif : peut être installé sans crainte.",
            RISK_MEDIUM: "Installer avec sauvegarde, puis vérifier que tout répond.",
            RISK_HIGH: "Lire les notes de version avant. Sauvegarde obligatoire.",
            RISK_BLOCKED: "Ne pas installer pour l'instant : incompatible avec ta version de Home Assistant.",
        }[risk]

        can_install = bool(features & FEATURE_INSTALL) and risk != RISK_BLOCKED
        updates.append(
            {
                "entity_id": state.entity_id,
                "title": title,
                "source": src,
                "source_label": SOURCE_LABEL.get(src, src),
                "installed": installed,
                "latest": latest,
                "jump": jump,
                "risk": risk,
                "reasons": reasons,
                "recommendation": recommendation,
                "release_url": attrs.get("release_url"),
                "can_install": can_install,
                "can_rollback": bool(features & FEATURE_SPECIFIC_VERSION),
                "in_progress": bool(attrs.get("in_progress")),
                "picture": attrs.get("entity_picture"),
            }
        )

    order = {RISK_BLOCKED: 0, RISK_HIGH: 1, RISK_MEDIUM: 2, RISK_LOW: 3}
    updates.sort(key=lambda u: (order.get(u["risk"], 9), u["title"]))

    if updates:
        safe = [u for u in updates if u["risk"] == RISK_LOW]
        risky = [u for u in updates if u["risk"] in (RISK_HIGH, RISK_BLOCKED)]
        sev = WARNING if len(updates) >= 5 or risky else INFO
        issues.append(
            issue(
                "updates_pending",
                CAT_UPDATES,
                sev,
                f"{len(updates)} mise(s) à jour en attente",
                detail=(
                    f"{len(safe)} sans risque, {len(updates) - len(safe) - len(risky)} à risque moyen, "
                    f"{len(risky)} à risque élevé ou bloquée(s)."
                ),
                fix="Ouvre l'onglet Mises à jour : chaque mise à jour a une analyse et un bouton d'installation sécurisée.",
                actions=[
                    action(
                        "update_all_safe",
                        f"Installer les {len(safe)} correctifs sûrs",
                        confirm=(
                            "Majordome va faire une sauvegarde, installer les correctifs à faible "
                            "risque un par un, puis vérifier que tout fonctionne. Continuer ?"
                        ),
                    )
                ]
                if safe
                else [],
            )
        )
    for upd in updates:
        if upd["source"] == "core":
            months = None
            a = _cal_months(parse_version(upd["installed"]))
            b = _cal_months(parse_version(upd["latest"]))
            if a is not None and b is not None:
                months = b - a
            if months and months >= 3:
                issues.append(
                    issue(
                        "core_outdated",
                        CAT_UPDATES,
                        WARNING if months < 6 else CRITICAL,
                        f"Home Assistant a {months} mois de retard",
                        detail=f"Installé : {upd['installed']} — disponible : {upd['latest']}.",
                        fix="Plus on attend, plus le saut est risqué. Majordome a vérifié les changements cassants pour toi.",
                    )
                )
    return updates, issues
