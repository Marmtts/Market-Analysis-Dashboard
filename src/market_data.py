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


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d",
                   max_retries: int = 3, retry_delay_sec: float = 2.0) -> pd.DataFrame:
    """Pobiera historyczne notowania z prostym mechanizmem ponawiania prób."""
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
