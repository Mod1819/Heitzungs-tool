"""Persistente Speicherung über den HA-Storage-Helper (.storage/smart_heating.config)."""
from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import SAVE_DELAY, STORAGE_KEY, STORAGE_MINOR_VERSION, STORAGE_VERSION
from .models import default_config, validate_config

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[dict[str, Any]], None]


class _VersionedStore(Store):
    async def _async_migrate_func(self, old_major, old_minor, old_data):
        # Noch keine Migrationen; Struktur wird beim Laden validiert/ergänzt.
        return old_data


class ConfigStore:
    """Hält die validierte Konfiguration im Speicher und persistiert Änderungen."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = _VersionedStore(
            hass, STORAGE_VERSION, STORAGE_KEY, minor_version=STORAGE_MINOR_VERSION,
            atomic_writes=True,
        )
        self._data: dict[str, Any] = default_config()
        self._listeners: list[Listener] = []
        self.load_error: str | None = None
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        if raw is None:
            _LOGGER.debug("Keine gespeicherte Konfiguration – starte mit Standardwerten")
            self._data = default_config()
            return
        try:
            self._data = validate_config(raw)
        except vol.Invalid as err:
            # Nichts verwerfen: Original sichern, dann mit Standardwerten weiter.
            self.load_error = str(err)
            _LOGGER.error(
                "Gespeicherte Konfiguration ungültig (%s). Original gesichert unter "
                ".storage/%s.invalid_backup", err, STORAGE_KEY,
            )
            backup = Store(self.hass, STORAGE_VERSION, f"{STORAGE_KEY}.invalid_backup")
            await backup.async_save(raw)
            self._data = default_config()

    @property
    def data(self) -> dict[str, Any]:
        """Kopie – Aufrufer können die interne Konfiguration nicht versehentlich ändern."""
        return copy.deepcopy(self._data)

    async def async_replace(self, new_data: dict[str, Any], *, immediate: bool = False) -> dict[str, Any]:
        """Neue Gesamtkonfiguration validieren, übernehmen und speichern.

        Wirft vol.Invalid; bei Fehler bleibt der alte Stand unverändert.
        """
        validated = validate_config(new_data)
        if validated == self._data:
            return self.data
        self._data = validated
        if immediate:
            await self._store.async_save(self._data)
        else:
            self._store.async_delay_save(lambda: self._data, SAVE_DELAY)
        for listener in list(self._listeners):
            try:
                listener(self.data)
            except Exception:  # noqa: BLE001 – ein Listener darf andere nicht blockieren
                _LOGGER.exception("Fehler in Konfigurations-Listener")
        return self.data

    async def async_update(
        self, mutator: Callable[[dict[str, Any]], dict[str, Any]], *, immediate: bool = False
    ) -> dict[str, Any]:
        """Atomares Lesen-Ändern-Schreiben (verhindert verlorene Updates).

        mutator erhält eine Kopie und gibt die neue Konfiguration zurück;
        wirft er eine Exception, bleibt alles unverändert.
        """
        async with self._lock:
            return await self.async_replace(mutator(self.data), immediate=immediate)

    async def async_flush(self) -> None:
        """Ausstehende verzögerte Speicherung sofort schreiben."""
        await self._store.async_save(self._data)

    async def async_remove(self) -> None:
        await self._store.async_remove()

    @callback
    def async_add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove
