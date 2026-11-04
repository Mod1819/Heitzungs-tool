# Smart Heating Control (Home Assistant Custom Integration)

Version **1.3.0** – Raumweise Heizungssteuerung mit Wochenprofilen, Fenster-offen-Sperre,
Außentemperaturregelung und eigener Oberfläche (Seitenleiste „Heizung“).

> ⚠️ Die Integration steuert Thermostate aktiv (`climate.set_temperature`). Vor dem Einsatz
> andere Heizungsautomationen/Blueprints für **dieselben Thermostate** deaktivieren – sonst regeln
> zwei Systeme gegeneinander. Empfehlung: zuerst mit einem Raum testen.

## Installation (HACS)

1. HACS → ⋮ oben rechts → **Benutzerdefinierte Repositories**
2. Repository: `https://github.com/Mod1819/Heitzungs-tool`, Typ **Integration** → Hinzufügen
3. In HACS „Smart Heating Control“ suchen → **Herunterladen**
4. Home Assistant neu starten
5. Einstellungen → Geräte & Dienste → Integration hinzufügen → **Smart Heating Control**
6. In der Seitenleiste erscheint **Heizung** (nur für Administratoren, ggf. Strg+F5)

Updates erscheinen danach automatisch in HACS.

## Erste Schritte (im Panel)

1. **Räume** → Raum anlegen → Thermostate und Fensterkontakte hinzufügen.
2. **Profile** → „Winter/Frühling/Sommer/Herbst anlegen“ → gewünschtes Profil aktivieren.
3. **Wochenplan** → Profil + Raum wählen → Abschnitte eintragen → „Kopieren nach Mo–Fr“ → Speichern.
4. Optional **Einstellungen** → Außentemperatursensor wählen, Regelung aktivieren, Kurve prüfen (Vorschautabelle).
5. **System** zeigt Gerätestatus und Probleme; jede Raumansicht die letzten Aktionen.

## Prioritäten (zentral in `targets.py`)

1. Sicherheits-/Fehlerzustand (Kontakt unbekannt → wie offen; Sensor ungültig → keine Absenkung)
2. Fenstersperre – wird durch nichts aufgehoben
3. Manueller Sollwert (bis zum nächsten Abschnitt oder dauerhaft) – wird bei offenem Fenster gespeichert
4. Wochenplan des aktiven Profils (Raum-Überschreibung vor globalem Profil), sonst Standardtemperatur
5. Außentemperatur-Absenkung (nur auf Plan-/Standardwerte)

Thermostat in mehreren Räumen: niedrigster effektiver Wert gewinnt; ein offenes Fenster in einem
der Räume sperrt es. Bedienung an einem solchen Thermostat gilt für alle seine Räume.

## Architektur

Reine **Custom Integration** (`custom_components/smart_heating`), kein Add-on.

- Läuft im HA-Prozess → direkter Zugriff auf State-Events (Thermostate, Fensterkontakte) ohne Token/WebSocket-Umweg.
- Startet/stoppt mit HA, keine Zusatzcontainer, keine Datenbank.
- Persistenz über HA-Storage-Helper: `.storage/smart_heating.config` (atomar geschrieben, versioniert).
- GUI später als eigenes Seitenleisten-Panel (`panel_custom`, Web-Component) + WebSocket-Befehle der Integration. Ein Add-on wäre nur nötig, wenn ein separater Webserver gebraucht würde – ist es nicht.
- Läuft auch auf HA Core/Container (Add-ons nur mit HA OS/Supervised).

## Struktur

```
custom_components/smart_heating/
  __init__.py             Setup/Unload, Verdrahtung aller Module
  config_flow.py          Einzelinstanz, keine Eingaben
  const.py · models.py    Konstanten, Datenmodell + Validierung (rein)
  store.py                Persistenz (.storage/smart_heating.config), einziger Schreibweg
  rooms.py · profiles.py  Raum-, Profil-, Wochenplan-, Handbetriebs-Operationen (rein)
  schedule.py             Zeitberechnung inkl. Sommer-/Winterzeit (rein)
  targets.py              Zentrale Prioritätenlogik (rein)
  scheduler.py            Ein Timer auf den nächsten Wechsel
  controller.py           Thermostatansteuerung, Fenster, Außensensor, Echo-Schutz
  notify.py               Fenster-Meldungen (Event + Dienst)
  websocket*.py           API (Räume, Profile/Pläne, Einstellungen, Status/Abo)
  panel.py · www/         Seitenleisten-Panel
tests/                    209 Python-Tests (HA-Core) + tests/js (jsdom)
```

## Datenmodell

- `settings`: aktives Profil, Außentemperatursensor + Kurve (12 → 20 °C, max. 3 K), Verhalten bei unbekanntem Fensterstatus (Standard: **wie offen**), Standardtemperatur.
- `rooms`: Name, beliebig viele `climate.*`, beliebig viele `binary_sensor.*`/`input_boolean.*` Fenster, optional Raumtemperatursensor, Fenster-offen-Temperatur (Standard 5 °C), Profil-Override, Außentemperaturmodus (`global`/`off`/`custom`).
- `profiles`: Name + je Raum ein Wochenplan (`mon`…`sun`, Abschnitte `start`/`end`/`temp`, `24:00` erlaubt, Überschneidungen werden abgelehnt).


## WebSocket-API (Modul 2)

Lesen: alle angemeldeten Benutzer. Ändern: nur Admins.

| Befehl | Parameter |
|---|---|
| `smart_heating/rooms/list` | – |
| `smart_heating/rooms/create` | `name`, optional `thermostats`, `window_sensors`, `temperature_sensor`, `window_open_temp`, `profile_override_id`, `outdoor_mode`, `outdoor_curve` |
| `smart_heating/rooms/update` | `room_id` + beliebige Felder (Teiländerung) |
| `smart_heating/rooms/delete` | `room_id` (entfernt auch Zeitpläne des Raums) |
| `smart_heating/rooms/assign` / `unassign` | `room_id`, `kind` (`thermostat`/`window_sensor`), `entity_id` |
| `smart_heating/devices/map` | – (Entity → Räume, zeigt geteilte Geräte) |

Fehlercodes: `entity_not_found`, `invalid_format`, `duplicate_name`, `room_not_found`, `profile_not_found`, `not_assigned`, `invalid_kind`, `invalid_field`, `not_loaded`, `unauthorized`.

## Festgelegte Regeln für geteilte Thermostate (Umsetzung in Modul 4)

1. Thermostat in mehreren Räumen → niedrigster effektiver Sollwert gewinnt.
2. Fenstersperre hat höchste Priorität.
3. Offenes Fenster in irgendeinem zugeordneten Raum sperrt das Thermostat (`windows_for_thermostat`).
4. Sollwerte bleiben pro Raum getrennt gespeichert.
5. Nach Schließen aller zugeordneten Fenster wird der niedrigste gültige Sollwert wiederhergestellt.

## Profile & Zeitsteuerung (Modul 3)

Schichten: `profiles.py` (Operationen) · `schedule.py` (reine Zeitberechnung) · `targets.py` (zentrale Sollwertermittlung) · `scheduler.py` (HA-Timer) · `websocket_profiles.py` (API).

**Gewünschte Temperatur pro Raum:** manueller Sollwert (gültig) > Wochenplan des wirksamen Profils (Raum-Überschreibung, sonst global) > `default_temp` (Lücken im Plan / kein Profil).

**Handbetrieb:** `until_next` (Standard) gilt bis zum nächsten Planwechsel; `permanent` bis zur nächsten manuellen Änderung oder `manual/clear`. Persistent im Abschnitt `overrides`. Gespeichert wird die *gewünschte* Temperatur – die Fenstersperre wird in Modul 4 erst danach in `targets.py` angewendet und kann dadurch nicht umgangen werden.

**Zeit:** Wanduhrzeit der HA-Zeitzone. Genau ein Timer auf den nächsten Wechsel (kein Polling). Sommerzeit: Grenze in der Lücke greift um 03:00. Winterzeit: Rückstellung und beide Vorkommen von 02:xx lösen Neuberechnung aus. Nach Neustart wird der aktuelle Abschnitt aus der Uhrzeit neu bestimmt; abgelaufene manuelle Werte werden entfernt.

| Befehl | Parameter |
|---|---|
| `profiles/list` | – |
| `profiles/create` | `name`, optional `copy_from` |
| `profiles/create_presets` | – (Winter, Frühling, Sommer, Herbst) |
| `profiles/rename` / `copy` | `profile_id`, `name` |
| `profiles/delete` | `profile_id` (aktive/verwendete Profile gesperrt) |
| `profiles/set_active` | `profile_id` oder `null` |
| `schedule/get` / `set_week` / `set_day` | `profile_id`, `room_id` (+ `week` / `day`, `slots`) |
| `schedule/copy_day` | `profile_id`, `room_id`, `source_day`, `target_days` |
| `schedule/copy_room` | `profile_id`, `source_room_id`, `target_room_ids` |
| `manual/set` | `room_id`, `temperature`, `mode` (`until_next`/`permanent`) |
| `manual/clear` | `room_id` |
| `targets` | – (aktuelle Sollwerte, Quelle, nächster Wechsel) |

Raum-Profilüberschreibung: `rooms/update {room_id, profile_override_id}`.

## Fenstersperre & Thermostatansteuerung (Modul 4)

⚠️ Ab dieser Version sendet die Integration `climate.set_temperature` an alle zugeordneten Thermostate. Vor dem Einsatz andere Heizungsautomationen für dieselben Thermostate deaktivieren, sonst regeln zwei Systeme gegeneinander.

**Effektive Temperatur** (`targets.resolve_effective`, zentrale Stelle):
- Raum gesperrt, wenn mindestens ein Kontakt offen ist – oder unbekannt/nicht verfügbar (Einstellung `unknown_window_policy`, Standard `open`).
- Gesperrt: `min(window_open_temp, gewünschte Temperatur)`; die gewünschte Temperatur bleibt unverändert gespeichert.
- Thermostat in mehreren Räumen: niedrigster effektiver Wert gewinnt → ein offenes Fenster in einem der Räume sperrt das Thermostat.

**Controller** (`controller.py`):
- Startet erst nach `homeassistant_started` (kein Senden mit unbekannten Zuständen).
- Sendet nur bei Abweichung; rundet auf `target_temp_step`, begrenzt auf `min_temp`/`max_temp`.
- Echo-Schutz: eigener Befehl gilt bis zur Bestätigung (max. 30 s), Zwischenwerte werden ignoriert; ohne Bestätigung max. 2 Wiederholungen, dann Status `not_confirmed`.
- Bedienung am Thermostat → manueller Sollwert (`until_next`) für alle Räume des Thermostats; bei offenem Fenster wird sofort wieder der Sperrwert gesendet.
- Nicht angesteuert: Thermostate im Modus `off`, ohne Einzelsollwert (`temperature`-Attribut) oder nicht verfügbar (wird nach Rückkehr nachgeholt).

`smart_heating/status` liefert Fensterzustände, Sperrgrund, gewünschte/effektive Temperaturen, Thermostatstatus und die letzten Aktionen pro Raum.

## Außentemperaturregelung (Modul 5)

Reihenfolge in `targets.resolve_effective`: **gewünscht → Außentemperatur-Absenkung → Fenstersperre**.

- Absenkung nur auf Plan-/Standardwerte; ein manueller Sollwert wird nicht abgesenkt.
- Linear: ≤ `start_at` → 0 K, ≥ `full_at` → `max_reduction`, dazwischen stufenlos.
- Optional `points` (Stützpunkte, z. B. `[[12,0],[14,0.5],[16,1],[18,2],[20,3]]`) für nicht-lineare Kurven; `points: null` schaltet zurück auf linear.
- Raum: `outdoor_mode` = `global` (globale Kurve), `custom` (eigene Kurve), `off`. Globaler Hauptschalter `outdoor_enabled`.
- Ergebnis nie unter 5 °C durch Absenkung; die Fenstersperre hat immer Vorrang.
- Sensor ungültig/nicht verfügbar/unplausibel (außerhalb −50…60 °C) → **keine Absenkung** (Heizen nach Plan), Status sichtbar. °F/K werden umgerechnet.
- Hysterese 0,5 K: kleinere Schwankungen lösen keine Neuberechnung aus.

| Befehl | Parameter |
|---|---|
| `settings/get` | – |
| `settings/update` | `outdoor_sensor`, `outdoor_enabled`, `outdoor_curve` (Teiländerung), `unknown_window_policy`, `default_temp` |

## Oberfläche (Modul 6)

Panel `www/smart-heating-panel.js`: Web-Component ohne Build-Schritt und ohne externe Bibliotheken,
HA-Farbvariablen (Hell/Dunkel), responsive bis Smartphone-Breite. Daten ausschließlich über die
WebSocket-API; `smart_heating/subscribe` liefert Konfiguration + Status live (kein Polling).
Laufende Eingaben werden durch Live-Updates nicht überschrieben. Alle Namen werden HTML-maskiert.

Ansichten: Übersicht · Räume/Raumdetail · Profile · Wochenplan-Editor (Tagesbalken, Tag kopieren,
Plan auf andere Räume übertragen) · Einstellungen (inkl. Kurvenvorschau) · System.

## Tests

```
pip install pytest-homeassistant-custom-component
SH_SNAPSHOT=/tmp/snapshot.json pytest          # 209 Tests, schreibt echten Status-Snapshot
npm install jsdom
SH_SNAPSHOT=/tmp/snapshot.json node --test tests/js/panel_render.test.js   # 17 Oberflächentests
```

## Öffnungsverzögerung & Fenster-Meldungen (v1.1)

Pro Raum in der Raumansicht → „Verhalten bei offenem Fenster“:

| Feld | Bedeutung |
|---|---|
| Sperre erst nach (Minuten) | Fenster muss so lange offen sein, bevor abgesenkt wird (z. B. Terrassentür 5–10). 0 = sofort. Kurzes Öffnen löst nichts aus. |
| Meldestufen | Ab v1.2 mehrstufig, siehe unten. Die alte Einstellung „Meldung nach X Minuten“ wird automatisch zur ersten Stufe. |
| Meldung, wenn wieder geschlossen | Entwarnung, sobald **alle** Kontakte zu sind (nicht verfügbar zählt nicht als zu). |

- Ein unbekannter oder nicht verfügbarer Kontakt sperrt weiterhin **sofort**, ohne Verzögerung.
- Bei einem gemeinsamen Kontakt gilt die Verzögerung des jeweiligen Raums. Beispiel: Die Terrassentür sperrt das Esszimmer sofort, das Wohnzimmer erst nach 5 Minuten.
- Die Öffnungsdauer kommt aus `last_changed` des Kontakts. Ein Reload der Integration verlängert die Wartezeit dadurch nicht.

**Weg 1 – direkt:** In den Einstellungen einen Dienst eintragen, z. B. `notify.telegram`, `telegram_bot.send_message` oder `notify.mobile_app_…`. Mit „Testnachricht senden“ lässt sich das prüfen. Gesendet wird `{title: "Heizung", message: …}`.

**Weg 2 – eigene Automation:** Das Event `smart_heating_window_alert` wird **immer** ausgelöst. Felder: `type` (`open_too_long`/`closed`), `room_id`, `room_name`, `message`, `open_sensors`, `open_minutes`, `desired_temp`, `effective_temp`, `locked`.

```yaml
automation:
  - alias: "Heizung: Fenster zu lange offen → Telegram"
    triggers:
      - trigger: event
        event_type: smart_heating_window_alert
        event_data:
          type: open_too_long
    actions:
      - action: telegram_bot.send_message
        data:
          title: "Heizung"
          message: "{{ trigger.event.data.message }}"
```

## Meldestufen & gekoppelte Kontakte (v1.2)

**Meldestufen** pro Raum (bis zu 10), z. B. Bad:

| Stufe | nach | Nachricht | Aktion |
|---|---|---|---|
| 1 | 5 min | ✓ | – |
| 2 | 10 min | ✓ | – |
| 3 | 20 min | ✓ | – |
| 4 | 30 min | ✓ | `cover.bad` (Rollo schließen, optional auf Position %) |

- Aktion: `cover.*` → `close_cover` bzw. `set_cover_position`, `script.*` / `scene.*` → `turn_on`.
- Das Rollo fährt **nicht** automatisch wieder hoch.
- Jede Stufe löst pro Öffnung genau einmal aus. Ab Stufe 2 heißt der Text „(2. Erinnerung)“ usw.
- Das Event `smart_heating_window_alert` enthält zusätzlich `stage`, `stages` und `action_entity`.
- ⚠️ Kein Rollo an Türen eintragen, durch die man hinausgeht (Aussperren).

**Gekoppelte Kontakte** („Mitsperren durch Kontakte anderer Räume“), z. B. Küche und Flur an die Terrassentür im Wohnzimmer:
- Jeder gekoppelte Kontakt hat eine **eigene** Verzögerung. Beispiel: Wohnzimmer nach 5 min, Küche und Flur nach 15 min.
- Der gekoppelte Raum behält seinen eigenen Plan und seine eigenen Fenster. Ein Raum ohne Fenster (Flur) funktioniert genauso.
- Ein gekoppelter Kontakt, der nicht verfügbar ist, sperrt **nicht**. Das Wohnzimmer selbst bleibt dabei weiter abgesichert.
- Meldungen kommen nur vom Raum, dem der Kontakt gehört. Der gekoppelte Raum zeigt „Gekoppelt gesperrt – Terrassentür (gekoppelt)“.

## Fensterkontakte & Batterien (v1.3)

- **Nur echte Öffnungssensoren:** Ein Fensterkontakt-Gerät hat oft mehrere `binary_sensor`, z. B. auch „Batterie niedrig“ oder „Sabotage“.
  - Die Auswahlliste zeigt Fenster- und Türsensoren zuerst und blendet solche Einträge aus.
  - Das Backend lehnt sie mit `not_a_window_sensor` ab, sonst würde z. B. eine schwache Batterie als „Fenster offen“ gewertet.
- **Batterieüberwachung:** Geprüft werden die Geräte der eingetragenen Thermostate und Fensterkontakte sowie alle batteriebetriebenen Geräte im **selben HA-Bereich**.
  - Damit sind auch die Heizkörperthermostate einer Homematic-IP-Heizgruppe abgedeckt, die selbst keine Batterie hat.
  - Als niedrig gilt: Batterie-`binary_sensor` steht auf „on“ oder der Wert liegt unter 20 %.
  - Anzeige: Warnung auf der Raumkarte und Tabelle im Reiter **System**.
  - Die Regelung selbst wird dadurch nicht beeinflusst.
- **Homematic IP:** Als Thermostat die Heizgruppe `climate.<raum>` eintragen und die Gruppe auf Modus `heat` (Manuell) stellen. Sonst überschreibt der Zeitplan aus der HmIP-App die Sollwerte.

## Batterien manuell & Zeiteingabe (v1.3.1)

- **Batterien manuell zuordnen:** Im Raum gibt es die Karte „Batterien (optional, manuell)“. Dort lassen sich
  `sensor`- oder `binary_sensor`-Entitäten (z. B. der Batterie-Eintrag eines Homematic-IP-Fensterkontakts) pro
  Raum eintragen (`battery_entities`). Zusätzlich werden Batterie-Attribute (`low_battery`, `battery_low`,
  `battery_level`, `battery`) der Thermostate und Fensterkontakte selbst ausgewertet. Binäre Batterie-Sensoren
  sind in `battery_entities` erlaubt und werden nicht als Fensterkontakt abgelehnt.
- **Flexible Uhrzeiten:** Im Wochenplan werden Eingaben wie `6`, `6:00`, `6.00`, `630` automatisch zu `06:00`
  (`24` zu `24:00`). Gespeichert wird weiterhin `HH:MM`.
- **Bessere Rückmeldung:** Ungültige Zeiten, „Ende vor Start“ und Überschneidungen werden rot markiert und mit einem
  Hinweis unter dem Tag erklärt. Fehler- und Erfolgsmeldungen bleiben beim Scrollen oben sichtbar. „+ Abschnitt“ bei
  einem bis 24:00 belegten Tag zeigt einen Hinweis statt eines Fehlers.
