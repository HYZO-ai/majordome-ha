"""Constantes de Majordome."""

from __future__ import annotations

DOMAIN = "majordome"
VERSION = "0.2.0"

PANEL_URL = "majordome"
PANEL_TITLE = "Majordome"
PANEL_ICON = "mdi:robot-happy-outline"
PANEL_ELEMENT = "majordome-panel"
STATIC_URL = "/majordome_static"

SIGNAL_REPORT_UPDATED = f"{DOMAIN}_report_updated"

STORAGE_KEY = f"{DOMAIN}.storage"
STORAGE_VERSION = 1

# Options
CONF_SCAN_INTERVAL = "scan_interval"
CONF_Z2M_TOPIC = "z2m_base_topic"
CONF_WATCHDOG = "zigbee_watchdog"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_AUTO_PATCH = "auto_patch"

DEFAULT_SCAN_INTERVAL = 15  # minutes
DEFAULT_Z2M_TOPIC = "zigbee2mqtt"
DEFAULT_WATCHDOG = True
DEFAULT_AUTO_PATCH = False

# Gravités
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"
SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
SEVERITY_PENALTY = {CRITICAL: 15, WARNING: 5, INFO: 1}

# Catégories
CAT_UPDATES = "updates"
CAT_LOGS = "logs"
CAT_ZIGBEE = "zigbee"
CAT_CONFIG = "config"
CAT_SYSTEM = "system"
CAT_REPAIRS = "repairs"

# Risque des mises à jour
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_BLOCKED = "blocked"
