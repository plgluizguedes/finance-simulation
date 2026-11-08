"""Calendário de pregões da B3, calculado por regra e ajustável por config/calendario_overrides.yaml.

Status: A_CONFERIR. Feriados fixos e móveis (Páscoa) mais 24 e 31 de dezembro sem pregão.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta


def easter(year: int) -> date:
    """Domingo de Páscoa (algoritmo gregoriano anônimo)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def base_holidays(year: int) -> set[date]:
    pascoa = easter(year)
    days = {
        date(year, 1, 1),
        pascoa - timedelta(days=48),  # Segunda de Carnaval
        pascoa - timedelta(days=47),  # Terça de Carnaval
        pascoa - timedelta(days=2),   # Sexta-feira Santa
        date(year, 4, 21),            # Tiradentes
        date(year, 5, 1),
        pascoa + timedelta(days=60),  # Corpus Christi
        date(year, 9, 7),
        date(year, 10, 12),
        date(year, 11, 2),
        date(year, 11, 15),
        date(year, 12, 24),
        date(year, 12, 25),
        date(year, 12, 31),
    }
    if year >= 2024:
        days.add(date(year, 11, 20))  # Consciência Negra, feriado nacional desde 2024
    return days


class Calendar:
    def __init__(self, closed: Iterable[date] = (), open_: Iterable[date] = ()):
        self.closed = set(closed)
        self.open = set(open_)
        self._cache: dict[int, set[date]] = {}

    def holidays(self, year: int) -> set[date]:
        if year not in self._cache:
            self._cache[year] = base_holidays(year)
        return self._cache[year]

    def is_trading_day(self, day: date) -> bool:
        if day in self.open:
            return True
        if day in self.closed:
            return False
        if day.weekday() >= 5:
            return False
        return day not in self.holidays(day.year)

    def next_day(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def prev_day(self, day: date) -> date:
        d = day - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def on_or_before(self, day: date) -> date:
        return day if self.is_trading_day(day) else self.prev_day(day)

    def sessions_between(self, after: date, until: date) -> list[date]:
        """Pregões em (after, until], em ordem cronológica."""
        out: list[date] = []
        d = after + timedelta(days=1)
        while d <= until:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def add_sessions(self, day: date, n: int) -> date:
        d = day
        step = 1 if n >= 0 else -1
        for _ in range(abs(n)):
            d = self.next_day(d) if step > 0 else self.prev_day(d)
        return d
