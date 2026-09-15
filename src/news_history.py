"""
news_history.py
------------------
Pobiera HISTORYCZNE newsy dla spółki (np. rok wstecz) przez Finnhub API
(darmowy klucz: https://finnhub.io/register).

W przeciwieństwie do news_sources.py (który daje tylko "migawkę" ostatnich
kilku-kilkunastu nagłówków), tutaj celowo pobieramy newsy w blokach
miesięcznych z całego wybranego okresu (domyślnie 365 dni), żeby móc
zaobserwować, jak zmieniał się sentyment wokół spółki na przestrzeni czasu -
np. czy w ostatnich miesiącach poprawia się, czy pogarsza względem wcześniej.

Endpoint: GET https://finnhub.io/api/v1/company-news?symbol=X&from=Y&to=Z&token=...
Uwaga: Finnhub w darmowym planie ma limit zapytań (domyślnie 60/min) - stąd
prosty throttling (sleep) między kolejnymi wywołaniami.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import requests

from .news_sources import Headline

logger = logging.getLogger("xtb_trend_watch.news_history")

FINNHUB_BASE_URL = "https://finnhub.io/api/v1/company-news"


def _month_ranges(days_back: int) -> list[tuple[date, date]]:
    """Dzieli okres [dziś - days_back, dziś] na kolejne ~30-dniowe kawałki
    (Finnhub radzi sobie z dowolnym zakresem dat, ale dzielimy na miesiące,
    żeby móc policzyć sentyment osobno dla każdego miesiąca)."""
    today = date.today()
    start = today - timedelta(days=days_back)

    ranges = []
    cursor = start
    while cursor < today:
        chunk_end = min(cursor + timedelta(days=30), today)
        ranges.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return ranges


def _fetch_one_month(ticker: str, api_key: str, start: date, end: date, max_per_month: int) -> list[Headline] | None:
    """Zwraca None przy 403 (spółka poza planem Finnhub - sygnał do
    przerwania CAŁEGO pobierania dla tego tickera), pustą listę przy innych
    błędach (spróbujemy ponownie w kolejnym cyklu, bez blokowania reszty)."""
    params = {"symbol": ticker, "from": start.isoformat(), "to": end.isoformat(), "token": api_key}
    try:
        resp = requests.get(FINNHUB_BASE_URL, params=params, timeout=20)
        if resp.status_code == 403:
            logger.warning(
                "Finnhub: dostęp zabroniony (403) dla %s - prawdopodobnie ten instrument "
                "nie jest objęty darmowym planem Finnhub (typowe dla spółek spoza głównych "
                "giełd US, np. GPW). Pomijam newsy historyczne dla tej spółki.", ticker
            )
            return None
        resp.raise_for_status()
        items = resp.json() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Finnhub: błąd pobierania newsów dla %s (%s - %s): %s", ticker, start, end, exc)
        return []

    headlines = []
    for item in items[:max_per_month]:
        title = item.get("headline", "")
        if not title:
            continue
        headlines.append(Headline(
            title=title, source=item.get("source", "Finnhub"),
            link=item.get("url", ""), published=str(item.get("datetime", "")),
        ))
    return headlines


def fetch_historical_news_by_month(
    ticker: str,
    api_key: str,
    days_back: int = 365,
    max_per_month: int = 4,
    request_delay_sec: float = 1.1,
) -> dict[str, list[Headline]]:
    """
    PRZYROSTOWA wersja: miesiące STARSZE niż bieżący są traktowane jako
    "zamknięte" (dane historyczne z Finnhub się dla nich nie zmieniają) i
    pobierane z Finnhub TYLKO RAZ - przy każdym kolejnym cyklu są czytane
    z lokalnego cache'u (db.py: news_cache), bez żadnego zapytania do API.
    Tylko BIEŻĄCY miesiąc (wciąż "otwarty", mogą się w nim pojawiać nowe
    newsy) jest odpytywany na świeżo przy każdym cyklu.

    To redukuje ~12 zapytań/spółkę/cykl do typowo 1 zapytania/spółkę/cykl
    (poza pierwszym, "rozgrzewającym" uruchomieniem) - główny koszt czasowy
    (throttling Finnhub) znika niemal całkowicie po pierwszym pełnym przebiegu.
    """
    from . import db  # import lokalny - unika cyklicznego importu na starcie modułu

    if not api_key:
        logger.warning("Brak klucza Finnhub API - pomijam newsy historyczne dla %s.", ticker)
        return {}

    current_month_key = date.today().strftime("%Y-%m")
    cached = db.get_cached_news_months(ticker)
    monthly_headlines: dict[str, list[Headline]] = {}
    fetched_any_new = False

    for start, end in _month_ranges(days_back):
        month_key = start.strftime("%Y-%m")

        # Miesiąc ZAMKNIĘTY i już w cache'u - czytamy lokalnie, ZERO zapytań do API.
        if month_key != current_month_key and month_key in cached:
            monthly_headlines[month_key] = [Headline(**h) for h in cached[month_key]]
            continue

        # Miesiąc bieżący (zawsze odświeżamy) albo brakujący w cache'u (pierwsze
        # uruchomienie, albo nowo dodana spółka) - pobieramy z Finnhub.
        result = _fetch_one_month(ticker, api_key, start, end, max_per_month)
        if result is None:  # 403 - ten instrument nigdy nie będzie dostępny, przerywamy całkiem
            return monthly_headlines
        monthly_headlines[month_key] = result
        fetched_any_new = True

        # Cache'ujemy TYLKO miesiące zamknięte - bieżący miesiąc zapisujemy też
        # (nadpisując), żeby przy kolejnym uruchomieniu w tym samym miesiącu
        # mieć chociaż punkt startowy, ale i tak zostanie odświeżony ponownie.
        db.save_news_month_to_cache(ticker, month_key, [h.__dict__ for h in result])
        time.sleep(request_delay_sec)  # throttling tylko dla FAKTYCZNIE wykonanych zapytań

    if not fetched_any_new:
        logger.info("Newsy historyczne dla %s: wszystkie miesiące z lokalnego cache'u, 0 zapytań do Finnhub.", ticker)

    return monthly_headlines
