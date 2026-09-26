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


def get_gpw_market_headlines(feed_url: str, max_total: int = 60) -> list[Headline]:
    """Pobiera OGÓLNY kanał RSS o GPW (domyślnie Bankier.pl - Giełda) - to
    uzupełniające źródło dla spółek z Warszawy, gdzie Finnhub (news_history.py)
    zwraca 403 (poza darmowym planem - typowe dla spółek spoza głównych
    giełd US) i yfinance (get_ticker_news) ma bardzo skąpe pokrycie.

    Świadomie NIE scrapujemy poszczególnych podstron spółek (bankier.pl/
    gielda/notowania/akcje/<TICKER>/wiadomosci) - próby pokazały, że są
    ochraniane (Incapsula/wyzwania JS) i takie podejście było już wcześniej
    rozważone i odrzucone jako zbyt kruche. Zamiast tego parsujemy JEDEN,
    ogólny kanał RSS (ten sam prosty mechanizm co newsy makro w
    get_macro_headlines) i filtrujemy go per-spółka w filter_headlines_
    for_company - bez zależności od struktury HTML którejkolwiek strony."""
    if not feed_url:
        return []
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Błąd pobierania kanału RSS GPW (%s): %s", feed_url, exc)
        return []

    headlines = []
    for entry in parsed.entries[:max_total]:
        title = entry.get("title", "")
        if not title:
            continue
        headlines.append(Headline(
            title=title, source="Bankier.pl (Giełda)",
            link=entry.get("link", ""), published=entry.get("published", entry.get("updated", "")),
        ))
    return headlines


_COMPANY_LEGAL_SUFFIXES = (
    " s.a.", " sa", " spółka akcyjna", " inc.", " inc", " corp.", " corp",
    " plc", " nv", " ag", " se", " ltd.", " ltd", " co.", " company", " limited",
)


def _strip_legal_suffix(name: str) -> str:
    """Usuwa przyrostek prawny z nazwy spółki ("Synektik S.A." -> "Synektik") -
    nagłówki newsów prawie nigdy nie powtarzają pełnej, formalnej nazwy z
    konfiguracji (np. "XTB S.A." w watchlist.name), więc dopasowanie bez tej
    normalizacji przegapiłoby większość trafień."""
    lowered = name.strip().lower()
    for suffix in _COMPANY_LEGAL_SUFFIXES:
        if lowered.endswith(suffix):
            return name.strip()[: -len(suffix)].strip()
    return name.strip()


def filter_headlines_for_company(headlines: list[Headline], *keywords: str) -> list[Headline]:
    """Filtruje OGÓLNY kanał (np. get_gpw_market_headlines) do nagłówków,
    które wspominają KONKRETNĄ spółkę - proste dopasowanie tekstowe (nazwa
    spółki bez przyrostka prawnego, bazowy symbol tickera) w miejsce
    kruchego scrapowania per-spółka. Słowa kluczowe krótsze niż 3 znaki są
    pomijane (np. bazowy symbol "PKO" jest OK, ale 1-2-literowe zostawiłyby
    zbyt dużo przypadkowych trafień)."""
    terms = {_strip_legal_suffix(k).lower() for k in keywords if k and len(k.strip()) >= 3}
    terms = {t for t in terms if len(t) >= 3}
    if not terms:
        return []
    return [h for h in headlines if any(term in h.title.lower() for term in terms)]


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
