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


def fetch_historical_news_by_month(
    ticker: str,
    api_key: str,
    days_back: int = 365,
    max_per_month: int = 4,
    request_delay_sec: float = 1.1,
) -> dict[str, list[Headline]]:
    """
    Zwraca słownik {"YYYY-MM": [Headline, ...]} obejmujący ~days_back dni
    wstecz. Limituje liczbę nagłówków na miesiąc do max_per_month (żeby nie
    zalać LLM setkami nagłówków - i tak liczymy analizę słownikową na
    WSZYSTKICH pobranych nagłówkach z danego miesiąca, a do LLM trafia tylko
    krótka próbka reprezentatywna).
    """
    if not api_key:
        logger.warning("Brak klucza Finnhub API - pomijam newsy historyczne dla %s.", ticker)
        return {}

    monthly_headlines: dict[str, list[Headline]] = {}

    for start, end in _month_ranges(days_back):
        month_key = start.strftime("%Y-%m")
        params = {
            "symbol": ticker,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "token": api_key,
        }
        try:
            resp = requests.get(FINNHUB_BASE_URL, params=params, timeout=20)
            if resp.status_code == 403:
                # Darmowy plan Finnhub zwykle nie obejmuje spółek spoza głównych
                # giełd amerykańskich (np. GPW). Nie ma sensu próbować kolejnych
                # 11 miesięcy - to nie jest błąd przejściowy, tylko ograniczenie planu.
                logger.warning(
                    "Finnhub: dostęp zabroniony (403) dla %s - prawdopodobnie ten "
                    "instrument nie jest objęty darmowym planem Finnhub (typowe dla "
                    "spółek spoza głównych giełd US, np. GPW). Pomijam newsy "
                    "historyczne dla tej spółki.", ticker
                )
                return {}
            resp.raise_for_status()
            items = resp.json() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Finnhub: błąd pobierania newsów dla %s (%s - %s): %s",
                            ticker, start, end, exc)
            items = []

        headlines = monthly_headlines.setdefault(month_key, [])
        for item in items[:max_per_month]:
            title = item.get("headline", "")
            if not title:
                continue
            headlines.append(
                Headline(
                    title=title,
                    source=item.get("source", "Finnhub"),
                    link=item.get("url", ""),
                    published=str(item.get("datetime", "")),
                )
            )

        time.sleep(request_delay_sec)  # prosty throttling pod darmowy limit Finnhub

    return monthly_headlines
