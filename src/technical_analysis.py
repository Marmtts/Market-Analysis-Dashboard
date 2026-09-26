"""
technical_analysis.py
----------------------
Liczy wskaźniki techniczne i ocenia, czy dany moment wygląda na "górkę"
trendu (czego chcemy unikać), czy raczej na rozsądny punkt wejścia
(np. cofnięcie w trendzie wzrostowym, wyjście z wyprzedania).

Wskaźniki:
- RSI (Relative Strength Index)
- Średnie kroczące (SMA krótka/długa) i relacja ceny do nich
- Odległość od maksimum 52-tygodniowego
- Cofnięcie od lokalnego szczytu (ostatnie ~20 sesji)
- Wstęgi Bollingera (pozycja ceny w paśmie)
- Wolumen (wykrycie anomalii)

Wynik: liczbowy score 0-1 (im wyżej, tym lepszy potencjalny punkt wejścia
z punktu widzenia analizy technicznej) + etykieta sygnału + lista powodów.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# Kara za bliskość 52-tyg. maksimum - wydzielona jako stała, żeby móc ją
# precyzyjnie COFNĄĆ dla ETF-ów w analysis_engine.py (patrz sekcja o ETF-ach),
# zamiast zgadywać, ile dokładnie odjąć.
AT_TOP_DISTANCE_PENALTY = 0.30


@dataclass
class TechnicalResult:
    ticker: str
    signal: str                  # "AT_TOP" | "GOOD_ENTRY" | "NEUTRAL" | "WEAK_TREND"
    score: float                 # 0.0 - 1.0
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def _bollinger(close: pd.Series, period: int, num_std: float):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower

def _atr(history: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range - standardowa miara typowej dziennej zmienności,
    używana tu do wyliczenia rozsądnej odległości stop-loss (patrz suggested
    stop-loss w analyze())."""
    high, low, close = history["High"], history["Low"], history["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(period).mean()

def analyze(ticker: str, history: pd.DataFrame, fifty_two_week_high: float,
            fifty_two_week_low: float, cfg: dict) -> TechnicalResult:
    close = history["Close"]
    volume = history["Volume"]

    rsi_period = cfg["rsi_period"]
    rsi = _rsi(close, rsi_period)
    last_rsi = float(rsi.iloc[-1])

    ma_short = close.rolling(cfg["ma_short"]).mean()
    ma_long = close.rolling(cfg["ma_long"]).mean()
    last_close = float(close.iloc[-1])
    last_ma_short = float(ma_short.iloc[-1]) if not np.isnan(ma_short.iloc[-1]) else None
    last_ma_long = float(ma_long.iloc[-1]) if not np.isnan(ma_long.iloc[-1]) else None

    uptrend = bool(last_ma_short and last_ma_long and last_ma_short > last_ma_long)

    dist_from_52w_high_pct = (fifty_two_week_high - last_close) / fifty_two_week_high * 100
    dist_from_52w_low_pct = (last_close - fifty_two_week_low) / fifty_two_week_low * 100

    # lokalny szczyt z ostatnich ~20 sesji (lub mniej, jeśli danych brak)
    lookback = min(20, len(close))
    recent_high = float(close.tail(lookback).max())
    pullback_from_recent_high_pct = (recent_high - last_close) / recent_high * 100

    upper_bb, mid_bb, lower_bb = _bollinger(close, cfg["bollinger_period"], cfg["bollinger_std"])
    last_upper = float(upper_bb.iloc[-1]) if not np.isnan(upper_bb.iloc[-1]) else None
    last_lower = float(lower_bb.iloc[-1]) if not np.isnan(lower_bb.iloc[-1]) else None
    bb_position = None
    if last_upper is not None and last_lower is not None and last_upper != last_lower:
        bb_position = (last_close - last_lower) / (last_upper - last_lower)  # 0=dolne pasmo,1=górne

    avg_volume = float(volume.tail(30).mean()) if len(volume) >= 5 else float(volume.mean())
    last_volume = float(volume.iloc[-1])
    volume_spike = avg_volume > 0 and (last_volume / avg_volume) >= cfg["volume_spike_multiplier"]

        # --- Wykrycie gwałtownego, jednorazowego spadku (odróżnienie od zdrowej korekty) ---
    # RSI-wyprzedanie i cofnięcie od szczytu wyglądają identycznie zarówno przy
    # stopniowej korekcie w trendzie wzrostowym (dobra okazja), jak i przy
    # nagłym krachu na złych newsach (fundamentalny szok) - bez tego rozróżnienia
    # narzędzie nagradzałoby oba scenariusze tak samo.
    # Zmiana ceny w OSTATNIEJ sesji (odróżnij od recent_change_pct niżej, który
    # patrzy kilka sesji wstecz) - używana głównie do heatmapy watchlisty.
    day_change_pct = None
    if len(close) >= 2:
        prev_close = float(close.iloc[-2])
        if prev_close:
            day_change_pct = (last_close - prev_close) / prev_close * 100

    decline_lookback = cfg.get("sharp_decline_lookback_days", 5)
    decline_threshold_pct = cfg.get("sharp_decline_threshold_pct", 15)
    sharp_decline = False
    recent_change_pct = 0.0
    if len(close) > decline_lookback:
        price_n_days_ago = float(close.iloc[-decline_lookback - 1])
        recent_change_pct = (last_close - price_n_days_ago) / price_n_days_ago * 100
        sharp_decline = recent_change_pct <= -decline_threshold_pct

    # --- ATR i sugerowany stop-loss (kalkulator wielkości pozycji na dashboardzie) ---
    atr_period = cfg.get("atr_period", 14)
    atr_stop_multiplier = cfg.get("atr_stop_multiplier", 2.0)
    atr_series = _atr(history, atr_period)
    last_atr = float(atr_series.iloc[-1]) if not np.isnan(atr_series.iloc[-1]) else None
    suggested_stop_loss = None
    stop_distance_pct = None
    if last_atr is not None:
        suggested_stop_loss = last_close - atr_stop_multiplier * last_atr
        stop_distance_pct = (last_close - suggested_stop_loss) / last_close * 100

    reasons: list[str] = []
    score = 0.5  # punkt startowy neutralny
    signal = "NEUTRAL"

    # --- Kara za bycie blisko szczytu 52-tygodniowego (unikanie "górek") ---
    if dist_from_52w_high_pct <= cfg["distance_from_high_52w_warning_pct"]:
        score -= AT_TOP_DISTANCE_PENALTY
        reasons.append(
            f"Cena jest zaledwie {dist_from_52w_high_pct:.1f}% poniżej 52-tyg. maksimum - "
            f"wysokie ryzyko kupna blisko szczytu."
        )

    # --- Kara za wykupienie wg RSI ---
    if last_rsi >= cfg["rsi_overbought"]:
        score -= 0.25
        reasons.append(f"RSI={last_rsi:.1f} wskazuje na wykupienie (>= {cfg['rsi_overbought']}).")
    elif last_rsi <= cfg["rsi_oversold"]:
        if sharp_decline:
            reasons.append(
                f"RSI={last_rsi:.1f} wskazuje na wyprzedanie, ale cena spadła o "
                f"{recent_change_pct:.1f}% w ciągu ostatnich {decline_lookback} sesji - to wygląda "
                f"na gwałtowny, jednorazowy spadek (np. reakcja na newsy), a NIE stopniowe "
                f"wyprzedanie w trendzie wzrostowym. Celowo NIE traktujemy tego jak okazji bez "
                f"ręcznej weryfikacji przyczyny spadku."
            )
        else:
            score += 0.15
            reasons.append(f"RSI={last_rsi:.1f} wskazuje na wyprzedanie - możliwa okazja, "
                            f"ale sprawdź, czy to nie jest spadający nóż.")
    else:
        reasons.append(f"RSI={last_rsi:.1f} w neutralnym zakresie.")

    # --- Premia za cofnięcie w trendzie wzrostowym (klasyczny "buy the dip") ---
    if uptrend:
        reasons.append("Trend: SMA krótka > SMA długa (układ wzrostowy).")
        if pullback_from_recent_high_pct >= cfg["min_pullback_from_recent_high_pct"] and not sharp_decline:
            score += 0.20
            reasons.append(
                f"Cena cofnęła się o {pullback_from_recent_high_pct:.1f}% od lokalnego "
                f"szczytu, przy zachowaniu trendu wzrostowego - to preferowany scenariusz wejścia."
            )
        elif sharp_decline:
            reasons.append(
                f"Formalnie SMA krótka > SMA długa (trend wzrostowy), ale to nieaktualne w świetle "
                f"świeżego, gwałtownego spadku ceny - traktuj ten 'trend wzrostowy' z dużą rezerwą."
            )
        else:
            reasons.append(
                f"Brak istotnego cofnięcia od lokalnego szczytu "
                f"({pullback_from_recent_high_pct:.1f}%) - cena wciąż blisko lokalnych maksimów."
            )
            score -= 0.10
    else:
        reasons.append("Trend: SMA krótka <= SMA długa (brak potwierdzonego trendu wzrostowego).")
        score -= 0.10

    # --- Pozycja w paśmie Bollingera ---
    if bb_position is not None:
        if bb_position >= 0.95:
            score -= 0.15
            reasons.append("Cena przy górnej wstędze Bollingera - podwyższone ryzyko cofnięcia.")
        elif bb_position <= 0.15:
            score += 0.10
            reasons.append("Cena blisko dolnej wstęgi Bollingera - potencjalne wsparcie.")

    # --- Wolumen ---
    if volume_spike:
        reasons.append(
            f"Wykryto anomalię wolumenu ({last_volume/avg_volume:.1f}x średniej z 30 sesji) - "
            f"sprawdź newsy, to często oznacza istotne wydarzenie."
        )

    score = float(np.clip(score, 0.0, 1.0))

    if sharp_decline:
        signal = "SHARP_DECLINE"
    elif dist_from_52w_high_pct <= cfg["distance_from_high_52w_warning_pct"] or last_rsi >= cfg["rsi_overbought"]:
        signal = "AT_TOP"
    elif score >= 0.65 and uptrend:
        signal = "GOOD_ENTRY"
    elif not uptrend and score < 0.5:
        signal = "WEAK_TREND"
    else:
        signal = "NEUTRAL"

    metrics = {
        "last_price": last_close,
        "day_change_pct": round(day_change_pct, 2) if day_change_pct is not None else None,
        "rsi": round(last_rsi, 2),
        "ma_short": round(last_ma_short, 2) if last_ma_short else None,
        "ma_long": round(last_ma_long, 2) if last_ma_long else None,
        "uptrend": uptrend,
        "dist_from_52w_high_pct": round(dist_from_52w_high_pct, 2),
        "dist_from_52w_low_pct": round(dist_from_52w_low_pct, 2),
        "pullback_from_recent_high_pct": round(pullback_from_recent_high_pct, 2),
        "bollinger_position_0to1": round(bb_position, 2) if bb_position is not None else None,
        "volume_spike": volume_spike,
        "recent_change_pct": round(recent_change_pct, 2),
        "sharp_decline": sharp_decline,
        "atr": round(last_atr, 2) if last_atr is not None else None,
        "suggested_stop_loss": round(suggested_stop_loss, 2) if suggested_stop_loss is not None else None,
        "stop_distance_pct": round(stop_distance_pct, 2) if stop_distance_pct is not None else None,
    }

    return TechnicalResult(ticker=ticker, signal=signal, score=score, reasons=reasons, metrics=metrics)


# =====================================================================
# WSPARCIE WIELU INTERWAŁÓW - potwierdzenie sygnału dziennego trendem
# tygodniowym (klasyczna zasada: handluj zgodnie z wyższym interwałem,
# używaj niższego do precyzyjnego wejścia).
# =====================================================================

def resample_to_weekly(history: pd.DataFrame) -> pd.DataFrame:
    """Zamienia dzienne świece OHLCV na tygodniowe (tydzień kończy się w piątek)."""
    weekly = history.resample("W-FRI").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(how="any")
    return weekly


def _weekly_trend_snapshot(weekly_history: pd.DataFrame, cfg: dict) -> dict:
    """Prosta ocena trendu na interwale tygodniowym: SMA krótka vs długa + RSI.
    Okresy są konfigurowalne osobno (domyślnie znacznie krótsze niż na dziennym,
    bo tygodni w roku jest mniej niż dni sesyjnych)."""
    close = weekly_history["Close"]

    weekly_ma_short_period = cfg.get("weekly_ma_short", 10)
    weekly_ma_long_period = cfg.get("weekly_ma_long", 40)
    weekly_rsi_period = cfg.get("weekly_rsi_period", 14)

    if len(close) < max(weekly_ma_long_period, weekly_rsi_period) + 1:
        return {"available": False}

    ma_short = close.rolling(weekly_ma_short_period).mean()
    ma_long = close.rolling(weekly_ma_long_period).mean()
    rsi = _rsi(close, weekly_rsi_period)

    last_ma_short = float(ma_short.iloc[-1])
    last_ma_long = float(ma_long.iloc[-1])
    last_rsi = float(rsi.iloc[-1])

    return {
        "available": True,
        "uptrend": last_ma_short > last_ma_long,
        "rsi": round(last_rsi, 2),
        "overbought": last_rsi >= cfg.get("rsi_overbought", 70),
    }


def analyze_multi_timeframe(ticker: str, daily_history: pd.DataFrame,
                             fifty_two_week_high: float, fifty_two_week_low: float,
                             cfg: dict) -> TechnicalResult:
    """Liczy zwykłą analizę dzienną (analyze()), a następnie DOPRECYZOWUJE
    sygnał na podstawie potwierdzenia z interwału tygodniowego:
    - GOOD_ENTRY + potwierdzenie tygodniowe (trend w górę, RSI tyg. nie wykupiony)
      -> sygnał zostaje GOOD_ENTRY, niewielka premia do score.
    - GOOD_ENTRY BEZ potwierdzenia tygodniowego (np. dzienne odbicie w trendzie
      spadkowym na tygodniowym) -> sygnał obniżony do NEUTRAL - to częsty
      scenariusz "pułapki byka" (bull trap), którego chcemy unikać.
    - AT_TOP pozostaje AT_TOP niezależnie od tygodniowego kontekstu (zasada
      bezpieczeństwa ma pierwszeństwo).
    """
    result = analyze(ticker, daily_history, fifty_two_week_high, fifty_two_week_low, cfg)

    if not cfg.get("use_weekly_confirmation", True):
        return result

    weekly_history = resample_to_weekly(daily_history)
    weekly = _weekly_trend_snapshot(weekly_history, cfg)
    result.metrics["weekly_confirmation_available"] = weekly.get("available", False)

    if not weekly.get("available", False):
        result.reasons.append("Brak wystarczającej historii do potwierdzenia trendu tygodniowego "
                               "(pomijam ten filtr).")
        return result

    result.metrics["weekly_uptrend"] = weekly["uptrend"]
    result.metrics["weekly_rsi"] = weekly["rsi"]

    if result.signal == "GOOD_ENTRY":
        if weekly["uptrend"] and not weekly["overbought"]:
            result.score = float(min(1.0, result.score + 0.10))
            result.reasons.append(
                f"Potwierdzenie z interwału tygodniowego: trend wzrostowy, RSI tyg.={weekly['rsi']:.1f} "
                f"(nie wykupiony) - wyższa wiarygodność sygnału dziennego."
            )
        else:
            result.signal = "NEUTRAL"
            result.score = float(max(0.0, result.score - 0.20))
            reason = "Sygnał dzienny NIE jest potwierdzony na interwale tygodniowym "
            if not weekly["uptrend"]:
                reason += "(trend tygodniowy nie jest wzrostowy)"
            else:
                reason += f"(RSI tygodniowy wykupiony: {weekly['rsi']:.1f})"
            reason += " - obniżono sygnał do NEUTRAL, żeby uniknąć typowej 'pułapki byka'."
            result.reasons.append(reason)

    return result

def compute_relative_strength(stock_history: pd.DataFrame, benchmark_history: pd.DataFrame,
                               lookback_days: int) -> dict | None:
    """Porównuje zwrot spółki ze zwrotem benchmarku w tym samym oknie czasowym -
    odróżnia 'rośnie, bo cały rynek rośnie' od 'rośnie SZYBCIEJ niż rynek'
    (prawdziwa siła względna, dobrze udokumentowany czynnik w analizie
    technicznej - np. metodologia CANSLIM)."""
    if len(stock_history) <= lookback_days or len(benchmark_history) <= lookback_days:
        return None
    stock_return = (stock_history["Close"].iloc[-1] / stock_history["Close"].iloc[-lookback_days - 1] - 1) * 100
    bench_return = (benchmark_history["Close"].iloc[-1] / benchmark_history["Close"].iloc[-lookback_days - 1] - 1) * 100
    return {
        "stock_return_pct": round(float(stock_return), 2),
        "benchmark_return_pct": round(float(bench_return), 2),
        "relative_strength_pct": round(float(stock_return - bench_return), 2),
    }