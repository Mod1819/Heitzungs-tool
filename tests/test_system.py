"""Modul 7 – Gesamttest: alle Module zusammen, simulierte Geräte, Neustarts, Störungen, Schleifenfreiheit."""
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.smart_heating.const import DOMAIN, STORAGE_KEY

TZ = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 2, 5, 0, tzinfo=TZ)  # Montag 05:00
WEEK = lambda day: {d: day for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}  # noqa: E731
DAY_WZ = [{"start": "00:00", "end": "06:00", "temp": 17}, {"start": "06:00", "end": "08:30", "temp": 21},
          {"start": "08:30", "end": "16:00", "temp": 18}, {"start": "16:00", "end": "22:00", "temp": 21},
          {"start": "22:00", "end": "24:00", "temp": 17}]


class Devices:
    """Simulierte Thermostate: übernehmen Befehle wie echte Geräte (Rundung, Grenzen, Eigenheiten)."""

    def __init__(self, hass):
        self.hass = hass
        self.calls: Counter = Counter()
        self.log: list = []
        self.offline: set = set()
        self.quirk: dict = {}       # eid -> Funktion(temp) -> gemeldeter Wert
        self.ignore: set = set()     # Gerät nimmt Befehle an, ändert aber nichts
        hass.services.async_register("climate", "set_temperature", self._handle)

    def add(self, eid, temp=19.0, step=0.5, lo=5, hi=30):
        self.hass.states.async_set(eid, "heat", {"temperature": temp, "current_temperature": 19,
                                                 "target_temp_step": step, "min_temp": lo, "max_temp": hi})

    async def _handle(self, call):
        eid, t = call.data["entity_id"], call.data["temperature"]
        self.calls[eid] += 1
        self.log.append((eid, t))
        if eid in self.offline or eid in self.ignore:
            return
        st = self.hass.states.get(eid)
        reported = self.quirk.get(eid, lambda x: x)(t)
        self.hass.states.async_set(eid, st.state, {**st.attributes, "temperature": reported})

    def temp(self, eid):
        return self.hass.states.get(eid).attributes["temperature"]

    async def user(self, eid, t):
        st = self.hass.states.get(eid)
        self.hass.states.async_set(eid, st.state, {**st.attributes, "temperature": t})
        await self.hass.async_block_till_done()


def _config(rooms=None):
    rooms = rooms or {
        "wz": {"name": "Wohnzimmer", "thermostats": ["climate.wz1", "climate.wz2"],
               "window_sensors": ["binary_sensor.wz_f1", "binary_sensor.terrasse"]},
        "ez": {"name": "Esszimmer", "thermostats": ["climate.ez", "climate.flur"],
               "window_sensors": ["binary_sensor.terrasse"], "window_open_temp": 6},
        "flur": {"name": "Flur", "thermostats": ["climate.flur"], "window_sensors": ["binary_sensor.tuer"]},
        "bad": {"name": "Bad", "thermostats": ["climate.bad"], "outdoor_mode": "off"},
    }
    allday = lambda t: WEEK([{"start": "00:00", "end": "24:00", "temp": t}])  # noqa: E731
    sched = {rid: WEEK(DAY_WZ) if rid == "wz" else allday(20) for rid in rooms}
    settings = {"active_profile_id": "winter", "outdoor_enabled": True, "outdoor_sensor": "sensor.aussen",
                "thermostat_changes": "adopt"}
    return {"settings": settings, "rooms": rooms,
            "profiles": {"winter": {"name": "Winter", "schedules": sched},
                         "sommer": {"name": "Sommer", "schedules": {r: allday(16) for r in rooms}}}}


class System:
    def __init__(self, hass, hass_storage, freezer):
        self.hass, self.storage, self.freezer = hass, hass_storage, freezer
        self.entry = None

    async def boot(self, config=None, outdoor="8", running=True, start=START):
        hass = self.hass
        await hass.config.async_set_time_zone("Europe/Berlin")
        self.freezer.move_to(start)
        self.storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY,
                                     "data": config or _config()}
        self.dev = Devices(hass)
        for rid_cfg in (config or _config())["rooms"].values():
            for e in rid_cfg["thermostats"]:
                if hass.states.get(e) is None:
                    self.dev.add(e)
            for w in rid_cfg.get("window_sensors", []):
                hass.states.async_set(w, "off")
        hass.states.async_set("sensor.aussen", outdoor, {"unit_of_measurement": "°C"})
        self.entry = MockConfigEntry(domain=DOMAIN, data={})
        self.entry.add_to_hass(hass)
        await self.start(running)

    async def start(self, running=True):
        if not running:
            self.hass.set_state(CoreState.not_running)
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()

    async def restart(self, before_start=None):
        """HA-Neustart: entladen (persistiert), neu laden im Startmodus, dann 'gestartet'."""
        assert await self.hass.config_entries.async_unload(self.entry.entry_id)
        await self.hass.async_block_till_done()
        calls_before = sum(self.dev.calls.values())
        await self.start(running=False)
        assert sum(self.dev.calls.values()) == calls_before, "Senden vor HA-Start"
        if before_start:
            await before_start()
        self.hass.set_state(CoreState.running)
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await self.hass.async_block_till_done()

    async def at(self, hh, mm=0, day=0):
        when = START.replace(hour=hh, minute=mm) + timedelta(days=day)
        self.freezer.move_to(when)
        async_fire_time_changed(self.hass, when)
        await self.hass.async_block_till_done()

    async def wait(self, seconds):
        when = datetime.now(TZ) + timedelta(seconds=seconds)
        self.freezer.move_to(when)
        async_fire_time_changed(self.hass, when)
        await self.hass.async_block_till_done()

    async def set(self, eid, state):
        st = self.hass.states.get(eid)
        self.hass.states.async_set(eid, state, st.attributes if st else {"unit_of_measurement": "°C"})
        await self.hass.async_block_till_done()

    def temps(self):
        return {e: self.dev.temp(e) for e in ("climate.wz1", "climate.wz2", "climate.ez", "climate.flur",
                                               "climate.bad")}

    @property
    def rt(self):
        return self.entry.runtime_data


@pytest.fixture
def system(hass, hass_storage, freezer):
    return System(hass, hass_storage, freezer)


async def test_full_day_all_modules(system):
    """Ein kompletter Montag: Plan, Außentemperatur, Fenster, Bedienung, Neustart, Profilwechsel."""
    s = system
    await s.boot(outdoor="8")
    assert s.temps() == {"climate.wz1": 17, "climate.wz2": 17, "climate.ez": 20, "climate.flur": 20,
                         "climate.bad": 20}
    await s.at(6)
    assert s.dev.temp("climate.wz1") == 21
    await s.set("binary_sensor.terrasse", "on")         # 1 Fenster, 4 Thermostate, 2 Räume
    assert s.temps() == {"climate.wz1": 5, "climate.wz2": 5, "climate.ez": 6, "climate.flur": 6,
                         "climate.bad": 20}
    await s.dev.user("climate.wz1", 23)                 # Bedienung bei offenem Fenster
    assert s.dev.temp("climate.wz1") == 5
    await s.at(7)
    await s.set("binary_sensor.terrasse", "off")
    assert s.temps() == {"climate.wz1": 23, "climate.wz2": 23, "climate.ez": 20, "climate.flur": 20,
                         "climate.bad": 20}
    await s.at(8, 30)                                    # manueller Wert läuft mit Planwechsel ab
    assert s.dev.temp("climate.wz1") == 18
    await s.set("sensor.aussen", "16")                   # milder Tag: −1,5 K, Bad ausgenommen
    assert s.temps() == {"climate.wz1": 16.5, "climate.wz2": 16.5, "climate.ez": 18.5,
                         "climate.flur": 18.5, "climate.bad": 20}
    await s.set("binary_sensor.tuer", "on")              # Flur-Tür: gemeinsames Thermostat gesperrt
    assert s.dev.temp("climate.flur") == 5 and s.dev.temp("climate.ez") == 18.5

    async def door_still_open():
        s.hass.states.async_set("binary_sensor.tuer", "on")
    await s.at(12)
    await s.restart(before_start=door_still_open)        # Neustart mit offener Tür
    assert s.dev.temp("climate.flur") == 5
    await s.set("binary_sensor.tuer", "off")
    assert s.dev.temp("climate.flur") == 18.5
    await s.at(16)
    assert s.dev.temp("climate.wz1") == 19.5
    store = s.rt.store
    await store.async_update(lambda c: {**c, "settings": {**c["settings"], "active_profile_id": "sommer"}})
    await s.hass.async_block_till_done()
    assert s.temps() == {"climate.wz1": 14.5, "climate.wz2": 14.5, "climate.ez": 14.5, "climate.flur": 14.5,
                         "climate.bad": 16}
    # Schleifenfreiheit: kein Thermostat hat mehr Befehle als Zustandswechsel des Tages rechtfertigen
    assert max(s.dev.calls.values()) <= 12, s.dev.calls


async def test_restart_preserves_manual_during_window_lock(system):
    s = system
    await s.boot()
    await s.at(6, 30)
    await s.set("binary_sensor.wz_f1", "on")
    await s.dev.user("climate.wz2", 22.5)
    assert s.rt.store.data["overrides"]["wz"]["temp"] == 22.5

    async def window_unavailable_at_boot():
        s.hass.states.async_set("binary_sensor.wz_f1", "unavailable")
    await s.restart(before_start=window_unavailable_at_boot)
    assert s.dev.temp("climate.wz1") == 5 and s.dev.temp("climate.wz2") == 5  # keine Freigabe
    assert s.rt.controller.status()["rooms"]["wz"]["reason"] == "window_unknown"
    await s.set("binary_sensor.wz_f1", "off")
    assert s.dev.temp("climate.wz1") == 22.5 and s.dev.temp("climate.wz2") == 22.5


async def test_many_rooms_parallel(system):
    rooms = {f"r{i}": {"name": f"Raum {i}", "thermostats": [f"climate.t{i}a", f"climate.t{i}b"],
                       "window_sensors": [f"binary_sensor.f{i}"]} for i in range(25)}
    s = system
    await s.boot(config=_config(rooms))
    assert all(s.dev.temp(f"climate.t{i}a") == 20 for i in range(25))
    for i in range(0, 25, 2):
        s.hass.states.async_set(f"binary_sensor.f{i}", "on")
    await s.hass.async_block_till_done()
    for i in range(25):
        assert s.dev.temp(f"climate.t{i}b") == (5 if i % 2 == 0 else 20)
    assert all(n <= 2 for n in s.dev.calls.values()), "Mehrfachaufrufe bei parallelen Räumen"


async def test_window_flapping_bounded_and_correct(system):
    s = system
    await s.boot()
    base = s.dev.calls["climate.wz1"]
    for _ in range(10):
        await s.set("binary_sensor.wz_f1", "on")
        await s.set("binary_sensor.wz_f1", "off")
    assert s.dev.calls["climate.wz1"] - base <= 20
    assert s.dev.temp("climate.wz1") == 17
    assert "wz" not in s.rt.store.data["overrides"]  # Echo nie als Bedienung gewertet


async def test_device_quirk_no_regulation_loop(system):
    """Gerät meldet +0,3 K Abweichung -> wird einmalig als Bedienung akzeptiert, keine Endlosschleife."""
    s = system
    s_quirk = lambda t: t + 0.3  # noqa: E731
    await s.boot()
    s.dev.quirk["climate.bad"] = s_quirk
    await s.at(6)
    await s.set("sensor.aussen", "20")
    await s.rt.scheduler.async_set_manual("bad", 22, "permanent")
    await s.hass.async_block_till_done()
    before = s.dev.calls["climate.bad"]
    for _ in range(20):
        await s.wait(31)
    assert s.dev.calls["climate.bad"] - before <= 1  # genau ein Befehl, kein Aufschaukeln
    assert s.dev.temp("climate.bad") == pytest.approx(22.3)
    assert s.rt.store.data["overrides"]["bad"]["temp"] == 22  # Wunschwert nicht "hochgerechnet"
    assert s.rt.controller.status()["thermostats"]["climate.bad"]["status"] == "ok"
    await s.dev.user("climate.bad", 19)  # echte Bedienung wird weiterhin erkannt
    assert s.rt.store.data["overrides"]["bad"]["temp"] == 19


async def test_unresponsive_and_offline_devices(system):
    s = system
    await s.boot()
    s.dev.ignore.add("climate.ez")
    s.dev.offline.add("climate.wz2")
    await s.set("binary_sensor.terrasse", "on")
    for _ in range(6):
        await s.wait(31)
    st = s.rt.controller.status()["thermostats"]
    assert st["climate.ez"]["status"] == "not_confirmed"
    assert s.dev.calls["climate.ez"] == 1 + 2 + 1  # Start + Sperrbefehl + 2 Wiederholungen
    await s.set("climate.wz2", "unavailable")
    assert s.rt.controller.status()["thermostats"]["climate.wz2"]["status"] == "unavailable"
    s.dev.offline.discard("climate.wz2")
    s.hass.states.async_set("climate.wz2", "heat", {**s.hass.states.get("climate.wz1").attributes,
                                                     "temperature": 17})
    await s.hass.async_block_till_done()
    assert s.dev.temp("climate.wz2") == 5  # nach Rückkehr sofort gesperrt
    assert s.dev.temp("climate.flur") == 6


async def test_outdoor_sensor_failure_and_recovery(system):
    s = system
    await s.boot(outdoor="18")
    assert s.dev.temp("climate.ez") == 18  # 20 − 2,25 → 17,8 → Schritt 0,5
    await s.set("sensor.aussen", "unavailable")
    assert s.dev.temp("climate.ez") == 20
    await s.set("sensor.aussen", "-200")
    assert s.dev.temp("climate.ez") == 20 and s.rt.controller.status()["outdoor"]["status"] == "implausible"
    await s.set("sensor.aussen", "18")
    assert s.dev.temp("climate.ez") == 18


async def test_dst_week_with_regulation(system):
    """Woche über die Zeitumstellung (25.10.2026): Wechsel bleiben pünktlich in Ortszeit."""
    s = system
    when = datetime(2026, 10, 24, 21, 0, tzinfo=TZ)
    await s.boot(start=when)
    for hours in range(0, 40):
        t = when + timedelta(hours=hours)
        s.freezer.move_to(t)
        async_fire_time_changed(s.hass, t)
        await s.hass.async_block_till_done()
        assert s.dev.temp("climate.wz1") == _expected_wz(t.astimezone(TZ).hour), t.astimezone(TZ)


def _expected_wz(hour):
    """Wohnzimmer-Plan zur vollen Stunde (Wechsel 06:00, 08:30, 16:00, 22:00)."""
    if hour < 6 or hour >= 22:
        return 17
    if hour < 9 or hour >= 16:
        return 21
    return 18
