"""Zeitberechnung inkl. Tageswechsel und Sommer-/Winterzeit (Europe/Berlin)."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from custom_components.smart_heating.models import WEEK_SCHEMA
from custom_components.smart_heating.schedule import current_slot, next_change

TZ = ZoneInfo("Europe/Berlin")
WORKDAY = [
    {"start": "00:00", "end": "06:00", "temp": 17}, {"start": "06:00", "end": "08:30", "temp": 21},
    {"start": "08:30", "end": "16:00", "temp": 18}, {"start": "16:00", "end": "22:00", "temp": 21},
    {"start": "22:00", "end": "24:00", "temp": 17},
]
WEEK = WEEK_SCHEMA({d: WORKDAY for d in ("mon", "tue", "wed", "thu", "fri")}
                   | {"sat": [{"start": "08:00", "end": "23:00", "temp": 22}]})


def loc(*a):
    return datetime(*a, tzinfo=TZ)


def test_current_slot_examples():
    assert current_slot(WEEK, loc(2026, 11, 2, 7, 0))["temp"] == 21   # Mo 07:00
    assert current_slot(WEEK, loc(2026, 11, 2, 8, 30))["temp"] == 18  # Grenze: Start inklusiv
    assert current_slot(WEEK, loc(2026, 11, 2, 23, 59))["temp"] == 17
    assert current_slot(WEEK, loc(2026, 11, 7, 7, 59)) is None        # Sa Lücke
    assert current_slot(WEEK, loc(2026, 11, 8, 12, 0)) is None        # So leer
    assert current_slot(None, loc(2026, 11, 2, 12, 0)) is None


def test_next_change_same_day_and_day_change():
    assert next_change(WEEK, loc(2026, 11, 2, 7, 0).astimezone(UTC), TZ) == loc(2026, 11, 2, 8, 30)
    # Mo 22:30 -> nächster Wechsel Di 00:00 (Ende 24:00 / Start 00:00)
    assert next_change(WEEK, loc(2026, 11, 2, 22, 30).astimezone(UTC), TZ) == loc(2026, 11, 3, 0, 0)
    # Fr 23:00 -> Sa 00:00 (Ende Abschnitt 22:00–24:00), dann Sa 00:30 -> Sa 08:00 (Lücke)
    assert next_change(WEEK, loc(2026, 11, 6, 23, 0).astimezone(UTC), TZ) == loc(2026, 11, 7, 0, 0)
    assert next_change(WEEK, loc(2026, 11, 7, 0, 30).astimezone(UTC), TZ) == loc(2026, 11, 7, 8, 0)
    # Sa 23:30 -> Mo 00:00 (Sonntag leer, Tageswechsel über 2 Tage)
    assert next_change(WEEK, loc(2026, 11, 7, 23, 30).astimezone(UTC), TZ) == loc(2026, 11, 9, 0, 0)


def test_next_change_empty():
    assert next_change(None, datetime(2026, 11, 2, tzinfo=UTC), TZ) is None
    assert next_change(WEEK_SCHEMA({}), datetime(2026, 11, 2, tzinfo=UTC), TZ) is None


DST_DAY = WEEK_SCHEMA({"sun": [{"start": "00:00", "end": "02:30", "temp": 17},
                               {"start": "02:30", "end": "24:00", "temp": 21}]})


def test_spring_forward_gap_boundary_fires_at_transition():
    # 29.03.2026: 02:00 CET -> 03:00 CEST (01:00 UTC); 02:30 existiert nicht
    now = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)  # 01:30 CET
    assert current_slot(DST_DAY, now.astimezone(TZ))["temp"] == 17
    nc = next_change(DST_DAY, now, TZ)
    assert nc == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert current_slot(DST_DAY, nc.astimezone(TZ))["temp"] == 21


def test_spring_forward_normal_times_shifted_correctly():
    now = datetime(2026, 3, 29, 2, 0, tzinfo=UTC)  # 04:00 CEST
    nc = next_change(WEEK, now, TZ)  # Montag 30.03. 06:00 CEST
    assert nc == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)  # Mo 00:00 CEST


def test_fall_back_repeated_hour():
    # 25.10.2026: 03:00 CEST -> 02:00 CET (01:00 UTC); 02:00–03:00 doppelt
    first = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 02:00 CEST
    assert next_change(DST_DAY, first, TZ) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    second = datetime(2026, 10, 25, 1, 15, tzinfo=UTC)  # 02:15 CET (zweites Mal)
    assert current_slot(DST_DAY, second.astimezone(TZ))["temp"] == 17
    # Rückstellung selbst ist Neuberechnungspunkt
    assert next_change(DST_DAY, datetime(2026, 10, 25, 0, 45, tzinfo=UTC), TZ) == datetime(
        2026, 10, 25, 1, 0, tzinfo=UTC)
    # zweites Vorkommen von 02:30 wird erkannt -> kein "Hängenbleiben" bis Mitternacht
    assert next_change(DST_DAY, second, TZ) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_dst_transitions_detected():
    from datetime import date

    from custom_components.smart_heating.schedule import dst_transitions
    assert dst_transitions(date(2026, 3, 29), TZ) == [datetime(2026, 3, 29, 1, 0, tzinfo=UTC)]
    assert dst_transitions(date(2026, 10, 25), TZ) == [datetime(2026, 10, 25, 1, 0, tzinfo=UTC)]
    assert dst_transitions(date(2026, 11, 3), TZ) == []
