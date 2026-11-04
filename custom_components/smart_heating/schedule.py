"""Zeitberechnung für Wochenpläne – rein, ohne HA-Abhängigkeit.

Regeln:
- Zeitpläne sind Wanduhrzeiten der HA-Zeitzone (Europe/Berlin).
- Der aktuelle Abschnitt wird immer aus der lokalen Wanduhrzeit bestimmt
  -> nach Neustart oder Zeitumstellung automatisch korrekt.
- Der nächste Wechselzeitpunkt wird als UTC-Zeitpunkt berechnet:
  * Frühjahr (Lücke 02:00–03:00): eine Grenze in der Lücke greift zum
    Umstellungszeitpunkt (03:00 Sommerzeit).
  * Herbst (02:00–03:00 doppelt): beide Vorkommen sind Wechselzeitpunkte,
    und der Umstellungszeitpunkt selbst löst eine Neuberechnung aus,
    damit nach der Rückstellung nichts "hängen bleibt".
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo

from .const import WEEKDAYS

DAY_MINUTES = 24 * 60


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def slot_at(day_slots: list[dict], minute_of_day: int) -> dict | None:
    """Abschnitt, der die Minute enthält (Start inklusiv, Ende exklusiv)."""
    for slot in day_slots:
        if minutes(slot["start"]) <= minute_of_day < minutes(slot["end"]):
            return slot
    return None


def current_slot(week: dict | None, now_local: datetime) -> dict | None:
    if not week:
        return None
    day = WEEKDAYS[now_local.weekday()]
    return slot_at(week.get(day, []), now_local.hour * 60 + now_local.minute)


def _wall_to_utc(day: date, minute_of_day: int, tz: tzinfo) -> list[datetime]:
    """Alle UTC-Zeitpunkte, zu denen die Wanduhr diesen Wert erreicht."""
    if minute_of_day >= DAY_MINUTES:  # 24:00 == 00:00 des Folgetags
        day, minute_of_day = day + timedelta(days=1), minute_of_day - DAY_MINUTES
    naive = datetime.combine(day, time(minute_of_day // 60, minute_of_day % 60))
    out = []
    for fold in (0, 1):
        cand = naive.replace(tzinfo=tz, fold=fold)
        utc = cand.astimezone(UTC)
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            out.append(utc)
    if not out:
        # Zeit existiert nicht (Lücke bei Umstellung auf Sommerzeit):
        # erster Zeitpunkt, an dem die Wanduhr diesen Wert überschritten hat.
        utc = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
        for _ in range(4 * 60):
            if utc.astimezone(tz).replace(tzinfo=None) >= naive:
                break
            utc += timedelta(minutes=1)
        out.append(utc)
    return sorted(set(out))


def dst_transitions(day: date, tz: tzinfo) -> list[datetime]:
    """UTC-Zeitpunkte einer Zeitumstellung an diesem lokalen Tag (meist keine)."""
    start = datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=tz).astimezone(UTC)
    if start.astimezone(tz).utcoffset() == end.astimezone(tz).utcoffset():
        return []
    lo, hi = start, end  # Binärsuche auf Minutenbasis
    while hi - lo > timedelta(minutes=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == lo.astimezone(tz).utcoffset():
            lo = mid
        else:
            hi = mid
    return [hi.replace(second=0, microsecond=0)]


def next_change(week: dict | None, now_utc: datetime, tz: tzinfo) -> datetime | None:
    """Nächster Zeitpunkt (UTC), an dem ein Abschnitt beginnt oder endet."""
    if not week or not any(week.get(d) for d in WEEKDAYS):
        return None
    today = now_utc.astimezone(tz).date()
    best: datetime | None = None
    for offset in range(-1, 9):  # -1: Grenzen um Mitternacht/Umstellung sicher erfassen
        day = today + timedelta(days=offset)
        # Umstellung selbst ist ein Wechselpunkt (Wanduhr springt, z. B. 03:00 -> 02:00)
        for cand in dst_transitions(day, tz):
            if cand > now_utc and (best is None or cand < best):
                best = cand
        for slot in week.get(WEEKDAYS[day.weekday()], []):
            for m in (minutes(slot["start"]), minutes(slot["end"])):
                for cand in _wall_to_utc(day, m, tz):
                    if cand > now_utc and (best is None or cand < best):
                        best = cand
        if best is not None and best < datetime.combine(
            day + timedelta(days=1), time(), tzinfo=tz
        ).astimezone(UTC):
            break
    return best
