"""
fx_rates.py
-------------
Pobieranie kursów wymiany walut (yfinance, pary FX np. "USDPLN=X") z prostym
cache'owaniem w pamięci (TTL 15 min) - żeby nie odpytywać Yahoo Finance przy
każdym cyklu i przy każdym sprawdzeniu alertu cenowego osobno.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import yfinance as yf

logger = logging.getLogger("xtb_trend_watch.fx_rates")

_CACHE: dict[tuple[str, str], tuple[float, float]] = {}  # (from,to) -> (rate, timestamp)
_TTL_SECONDS = 900  # 15 minut


def get_fx_rate(from_currency: str, to_currency: str) -> float | None:
    """Zwraca ile jednostek to_currency odpowiada 1 jednostce from_currency.
    Zwraca None, jeśli nie udało się pobrać kursu (np. nietypowa para) -
    wywołujący musi to obsłużyć (pominąć tę walutę, nie zgadywać kursu 1:1)."""
    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    if from_currency == to_currency:
        return 1.0

    cache_key = (from_currency, to_currency)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[1]) < _TTL_SECONDS:
        return cached[0]

    rate = _fetch_pair(f"{from_currency}{to_currency}=X")
    if rate is None:
        inverse = _fetch_pair(f"{to_currency}{from_currency}=X")
        rate = (1.0 / inverse) if inverse else None

    if rate is not None:
        _CACHE[cache_key] = (rate, now)
    else:
        logger.warning("Nie udało się pobrać kursu %s -> %s.", from_currency, to_currency)
    return rate


def _fetch_pair(pair_symbol: str) -> float | None:
    try:
        hist = yf.Ticker(pair_symbol).history(period="5d", interval="1d")
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def get_historical_fx_rate(from_currency: str, to_currency: str, date_str: str) -> float | None:
    """Zwraca PRZYBLIŻONY kurs z konkretnego dnia ("YYYY-MM-DD") - kurs
    RYNKOWY z yfinance (najbliższa dostępna sesja), NIE oficjalny średni
    kurs NBP wymagany polskimi przepisami podatkowymi. Używane wyłącznie
    do orientacyjnego podsumowania podatkowego (patrz report.compute_tax_summary) -
    przed rozliczeniem PIT zweryfikuj dokładne kwoty w tabelach NBP."""
    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    if from_currency == to_currency:
        return 1.0
    rate = _fetch_pair_historical(f"{from_currency}{to_currency}=X", date_str)
    if rate is None:
        inverse = _fetch_pair_historical(f"{to_currency}{from_currency}=X", date_str)
        rate = (1.0 / inverse) if inverse else None
    return rate


def _fetch_pair_historical(pair_symbol: str, date_str: str) -> float | None:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        start = (target - timedelta(days=7)).strftime("%Y-%m-%d")
        end = (target + timedelta(days=1)).strftime("%Y-%m-%d")
        hist = yf.Ticker(pair_symbol).history(start=start, end=end, interval="1d")
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        valid = closes[closes.index.strftime("%Y-%m-%d") <= date_str]
        return float(valid.iloc[-1]) if not valid.empty else float(closes.iloc[0])
    except Exception:  # noqa: BLE001
        return None