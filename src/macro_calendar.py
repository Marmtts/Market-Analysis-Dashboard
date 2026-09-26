"""
macro_calendar.py
-------------------
Kalendarz najbliższych, ZAPLANOWANYCH wydarzeń makro (posiedzenia FOMC i
RPP/NBP z decyzją o stopach, publikacje CPI) - kontekst "co się wydarzy w
najbliższych dniach", uzupełniający migawkę bieżących wartości VIX/rentowność
z macro_context.py.

Źródło to STATYCZNA lista dat w config.yaml (macro_calendar.events),
publikowana z wyprzedzeniem przez oficjalne instytucje (Fed, NBP, BLS) - z
reguły na cały rok naprzód. Świadomie NIE scrapujemy kalendarza z zewnętrznych
stron (np. investing.com) - to dokładnie ten sam kompromis, co przy GPW RSS
(patrz news_sources.py): kruche, zależne od struktury HTML, przeciw ToS wielu
serwisów. Lista w configu wymaga ręcznej aktualizacji raz na jakiś czas
(instytucje publikują harmonogram na kolejny rok zwykle pod jego koniec), ale
jest w pełni lokalna, szybka i niezawodna.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

DEFAULT_DAYS_AHEAD = 14


def get_upcoming_events(cfg: dict, today: date | None = None) -> list[dict]:
    """Zwraca wydarzenia z config.yaml (macro_calendar.events) mieszczące się
    w oknie [dziś, dziś + days_ahead] dni, posortowane rosnąco wg daty."""
    events = cfg.get("events") or []
    days_ahead = cfg.get("days_ahead", DEFAULT_DAYS_AHEAD)
    today = today or date.today()
    horizon = today + timedelta(days=days_ahead)

    upcoming = []
    for e in events:
        if not isinstance(e, dict):
            continue
        try:
            event_date = datetime.strptime(str(e.get("date")), "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= event_date <= horizon:
            upcoming.append({
                "date": event_date.isoformat(),
                "kind": e.get("kind", "?"),
                "label": e.get("label", ""),
                "days_away": (event_date - today).days,
            })

    upcoming.sort(key=lambda x: x["date"])
    return upcoming
