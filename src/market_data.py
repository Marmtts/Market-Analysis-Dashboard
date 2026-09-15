"""
market_data.py
----------------
Pobieranie danych cenowych (OHLCV) dla spółek z Yahoo Finance (yfinance).
Yahoo Finance ma pokrycie globalne (USA, Europa, Azja), co pasuje do
wymogu "spółki z całego świata".

Uwaga: yfinance korzysta z publicznego, nieoficjalnego API Yahoo Finance.
Bywa czasem niestabilne / ograniczane rate-limitami - stąd retry poniżej.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

logger = logging.getLogger("xtb_trend_watch.market_data")


@dataclass
class TickerData:
    ticker: str
    history: pd.DataFrame  # kolumny: Open, High, Low, Close, Volume
    fifty_two_week_high: float
    fifty_two_week_low: float
    last_price: float
    currency: str = "USD"  # waluta notowania (np. USD, PLN, EUR) - z yfinance fast_info


def _fetch_currency(ticker: str) -> str:
    """Pobiera walutę notowania (fast_info jest dużo szybsze niż pełne .info,
    bo nie ściąga wszystkich danych fundamentalnych - tylko potrzebujemy tu
    jednego pola). Domyślnie USD, jeśli się nie uda (np. chwilowy błąd API) -
    bezpieczny fallback zamiast wywalania całej analizy z tego powodu."""
    try:
        fast_info = yf.Ticker(ticker).fast_info
        try:
            currency = fast_info["currency"]
        except (KeyError, TypeError):
            currency = getattr(fast_info, "currency", None)
        return currency or "USD"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać waluty dla %s: %s - zakładam USD.", ticker, exc)
        return "USD"


# Cache w pamięci procesu (nie w SQLite - dane cenowe zmieniają się codziennie,
# więc trwały cache na dysku miałby minimalną wartość, a dodałby złożoność).
# Chroni przed WIELOKROTNYM pobieraniem TEJ SAMEJ spółki w krótkim odstępie
# czasu w ramach jednego "przebiegu" dashboardu - np. pełny cykl analizy +
# odświeżenie na żywo tej samej karty + alert cenowy mogą dziś niezależnie
# odpytać yfinance o te same dane w ciągu kilku minut.
_history_cache: dict[tuple[str, str, str], tuple[pd.DataFrame, float]] = {}
_HISTORY_CACHE_TTL_SECONDS = 300  # 5 minut - krótko, żeby NIGDY nie pokazać
                                    # istotnie nieaktualnej ceny, tylko oszczędzić
                                    # powtórki w obrębie tej samej "paczki" akcji.


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d",
                   max_retries: int = 3, retry_delay_sec: float = 2.0,
                   use_cache: bool = True) -> pd.DataFrame:
    """Pobiera historyczne notowania z prostym mechanizmem ponawiania prób.
    use_cache=False wymusza świeże pobranie - używane tam, gdzie absolutna
    świeżość jest ważniejsza niż oszczędność zapytań (np. alert cenowy)."""
    cache_key = (ticker, period, interval)
    if use_cache:
        cached = _history_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < _HISTORY_CACHE_TTL_SECONDS:
            return cached[0]

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
            if df is None or df.empty:
                raise ValueError(f"Brak danych historycznych dla {ticker}")
            df = df.dropna(how="all")
            # Najnowsza świeca (dzisiejsza/ostatnia sesja) czasem wraca z Yahoo
            # z wypełnionym Volume, ale NaN w Open/High/Low/Close (dane jeszcze
            # nierozliczone / sesja w toku). dropna(how="all") tego nie łapie,
            # bo Volume nie jest NaN - a taki wiersz psuje KAŻDE dalsze
            # obliczenie (RSI, SMA, ostatnia cena), bo close.iloc[-1] = NaN
            # przepływa przez cały pipeline. Usuwamy więc każdy wiersz bez
            # kompletnych danych cenowych, nawet jeśli wolumen jest obecny.
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                raise ValueError(f"Brak kompletnych danych OHLC dla {ticker} po odfiltrowaniu NaN")
            _history_cache[cache_key] = (df, time.time())
            return df
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Próba %s/%s pobrania danych dla %s nie powiodła się: %s",
                            attempt, max_retries, ticker, exc)
            time.sleep(retry_delay_sec)
    raise RuntimeError(f"Nie udało się pobrać danych dla {ticker}: {last_exc}")


def get_ticker_data(ticker: str, period: str = "1y", interval: str = "1d") -> TickerData:
    """Zwraca ustrukturyzowane dane dla spółki, w tym 52-tygodniowe ekstrema."""
    history = fetch_history(ticker, period=period, interval=interval)

    # Jeżeli poprosiliśmy o mniej niż rok, i tak spróbujmy dociągnąć pełny rok
    # do wyznaczenia poprawnego zakresu 52-tygodniowego.
    if period not in ("1y", "2y", "5y", "max"):
        try:
            year_hist = fetch_history(ticker, period="1y", interval="1d")
        except Exception:  # noqa: BLE001
            year_hist = history
    else:
        year_hist = history

    fifty_two_week_high = float(year_hist["High"].max())
    fifty_two_week_low = float(year_hist["Low"].min())
    last_price = float(history["Close"].iloc[-1])

    return TickerData(
        ticker=ticker,
        history=history,
        fifty_two_week_high=fifty_two_week_high,
        fifty_two_week_low=fifty_two_week_low,
        last_price=last_price,
        currency=_fetch_currency(ticker),
    )

import time

_benchmark_cache: dict[str, tuple[pd.DataFrame, float]] = {}
_BENCHMARK_CACHE_TTL = 3600  # 1h - benchmark jest wspólny dla wielu spółek w tym samym cyklu,
                              # nie ma sensu pobierać SPY osobno dla każdej z 19 spółek w watchliście


def fetch_benchmark_history(benchmark_ticker: str, period: str = "1y") -> pd.DataFrame:
    now = time.time()
    cached = _benchmark_cache.get(benchmark_ticker)
    if cached and (now - cached[1]) < _BENCHMARK_CACHE_TTL:
        return cached[0]
    hist = fetch_history(benchmark_ticker, period=period, interval="1d")
    _benchmark_cache[benchmark_ticker] = (hist, now)
    return hist