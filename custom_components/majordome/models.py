"""Petits utilitaires partagés : problèmes, actions, versions."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def issue(
    issue_id: str,
    category: str,
    severity: str,
    title: str,
    detail: str = "",
    fix: str = "",
    actions: list[dict[str, Any]] | None = None,
    entities: list[str] | None = None,
    link: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construit un problème affichable par le panneau."""
    data: dict[str, Any] = {
        "id": issue_id,
        "category": category,
        "severity": severity,
        "title": title,
        "detail": detail,
        "fix": fix,
        "actions": actions or [],
        "entities": entities or [],
        "link": link,
    }
    if extra:
        data.update(extra)
    return data


def action(
    action_id: str,
    label: str,
    params: dict[str, Any] | None = None,
    confirm: str | None = None,
) -> dict[str, Any]:
    """Construit un bouton d'action."""
    return {
        "action": action_id,
        "label": label,
        "params": params or {},
        "confirm": confirm,
    }


_VERSION_RE = re.compile(r"\d+")


def parse_version(value: Any) -> tuple[int, ...] | None:
    """'v2.1.8.2' -> (2, 1, 8, 2). None si illisible."""
    if value is None:
        return None
    text = str(value).strip().lstrip("vV")
    # On ignore les suffixes de build type "-beta", "+abc" pour la comparaison
    text = re.split(r"[-+ ]", text, maxsplit=1)[0]
    parts = _VERSION_RE.findall(text)
    if not parts:
        return None
    return tuple(int(p) for p in parts[:4])


def version_jump(installed: Any, latest: Any) -> str:
    """Retourne 'major', 'minor', 'patch' ou 'unknown'."""
    a = parse_version(installed)
    b = parse_version(latest)
    if not a or not b:
        return "unknown"
    a = a + (0,) * (3 - len(a))
    b = b + (0,) * (3 - len(b))
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return text.strip()


def integration_from_logger(name: str) -> str | None:
    """'homeassistant.components.met.coordinator' -> 'met'."""
    for prefix in ("homeassistant.components.", "custom_components."):
        if name.startswith(prefix):
            return name[len(prefix) :].split(".")[0]
    return None
