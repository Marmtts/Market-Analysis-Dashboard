"""
news_sources.py
-----------------
Pobiera nagłówki wiadomości z dwóch źródeł:
1) Newsy per-spółka (yfinance Ticker.news) - szybkie, ustandaryzowane.
2) Ogólnorynkowe kanały RSS (CNBC, MarketWatch, Investing.com, Yahoo Finance)
   - dają obraz "nastroju rynku" (makro), niezależnie od konkretnej spółki.

Zwracamy proste struktury: listę nagłówków (tytuł + źródło + link + data),
które trafią później do modułu sentymentu (LLM lub słownikowego fallbacku).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import feedparser
import yfinance as yf

logger = logging.getLogger("xtb_trend_watch.news_sources")


@dataclass
class Headline:
    title: str
    source: str
    link: str = ""
    published: str = ""


def get_ticker_news(ticker: str, max_headlines: int = 8) -> list[Headline]:
    """Pobiera newsy powiązane z konkretnym tickerem (yfinance)."""
    headlines: list[Headline] = []
    try:
        raw_news = yf.Ticker(ticker).news or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać newsów dla %s: %s", ticker, exc)
        return headlines

    for item in raw_news[:max_headlines]:
        # yfinance zwraca różne struktury w zależności od wersji - obsłużmy obie
        content = item.get("content", item)
        title = content.get("title") or item.get("title", "")
        publisher = (
            content.get("provider", {}).get("displayName")
            if isinstance(content.get("provider"), dict)
            else item.get("publisher", "")
        )
        link = (
            content.get("canonicalUrl", {}).get("url")
            if isinstance(content.get("canonicalUrl"), dict)
            else item.get("link", "")
        )
        pub_date = content.get("pubDate", item.get("providerPublishTime", ""))
        if title:
            headlines.append(Headline(title=title, source=publisher or "yfinance",
                                       link=link or "", published=str(pub_date)))
    return headlines


def get_macro_headlines(feeds_cfg: list[dict], max_total: int = 15) -> list[Headline]:
    """Pobiera ogólnorynkowe nagłówki z listy kanałów RSS zdefiniowanych w config.yaml."""
    headlines: list[Headline] = []
    per_feed_limit = max(1, max_total // max(1, len(feeds_cfg)))

    for feed_cfg in feeds_cfg:
        name = feed_cfg["name"]
        url = feed_cfg["url"]
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:per_feed_limit]:
                headlines.append(
                    Headline(
                        title=entry.get("title", ""),
                        source=name,
                        link=entry.get("link", ""),
                        published=entry.get("published", entry.get("updated", "")),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Błąd pobierania kanału RSS %s (%s): %s", name, url, exc)

    return headlines[:max_total]
