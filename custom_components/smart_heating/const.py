"""Konstanten für Smart Heating Control."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "smart_heating"
NAME: Final = "Smart Heating Control"

# Persistente Speicherung (.storage/<key>)
STORAGE_KEY: Final = f"{DOMAIN}.config"
STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 1
SAVE_DELAY: Final = 2  # Sekunden, bündelt schnelle Folgeänderungen

WEEKDAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Temperaturgrenzen (°C, intern immer Celsius)
DEFAULT_WINDOW_OPEN_TEMP: Final = 5.0
DEFAULT_COMFORT_TEMP: Final = 20.0
ABS_MIN_TEMP: Final = 4.0
ABS_MAX_TEMP: Final = 35.0

# Außentemperaturregelung (Standardwerte aus Spezifikation)
DEFAULT_OUTDOOR_START: Final = 12.0
DEFAULT_OUTDOOR_FULL: Final = 20.0
DEFAULT_OUTDOOR_MAX_REDUCTION: Final = 3.0

# Verhalten bei nicht verfügbarem Fensterkontakt
UNKNOWN_AS_OPEN: Final = "open"      # sicher: Sperre aktiv (Standard)
UNKNOWN_AS_CLOSED: Final = "closed"  # nur auf ausdrücklichen Wunsch
UNKNOWN_POLICIES: Final = (UNKNOWN_AS_OPEN, UNKNOWN_AS_CLOSED)

# Raumweise Außentemperaturregelung
OUTDOOR_MODE_GLOBAL: Final = "global"
OUTDOOR_MODE_OFF: Final = "off"
OUTDOOR_MODE_CUSTOM: Final = "custom"
OUTDOOR_MODES: Final = (OUTDOOR_MODE_GLOBAL, OUTDOOR_MODE_OFF, OUTDOOR_MODE_CUSTOM)
