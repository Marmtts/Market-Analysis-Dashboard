import numpy as np
import pandas as pd
import pytest

from src.technical_analysis import (
    _rsi,
    analyze,
    analyze_multi_timeframe,
    compute_relative_strength,
    resample_to_weekly,
)

# Okresy celowo mniejsze niż w config_example.yaml (ma_long=200 itd.) - te same
# klucze i ta sama logika, tylko dane testowe mogą zostać małe i czytelne.
CFG = {
    "rsi_period": 14,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "ma_short": 5,
    "ma_long": 20,
    "distance_from_high_52w_warning_pct": 5,
    "min_pullback_from_recent_high_pct": 3,
    "bollinger_period": 20,
    "bollinger_std": 2,
    "volume_spike_multiplier": 2.0,
    "sharp_decline_lookback_days": 5,
    "sharp_decline_threshold_pct": 15,
    "atr_period": 14,
    "atr_stop_multiplier": 2.0,
    "weekly_ma_short": 3,
    "weekly_ma_long": 8,
    "weekly_rsi_period": 5,
    "use_weekly_confirmation": True,
}


def make_history(closes, volumes=None):
    """Syntetyczne OHLCV na dniach roboczych - High/Low jako +-1% od Close
    (na potrzeby tych testów nic nie zależy od realnego zakresu świecy poza
    ATR), Open = poprzednie Close."""
    n = len(closes)
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(closes, index=idx, dtype=float)
    volume = pd.Series(volumes if volumes is not None else [1_000_000.0] * n, index=idx)
    high = close * 1.01
    low = close * 0.99
    open_ = close.shift(1).fillna(close.iloc[0])
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})


def zigzag_trend(start, end, n, amplitude=2.5):
    """Trend z oscylacją zamiast idealnie prostej linii - prosta linia pcha
    RSI do ekstremów (blisko 0 albo 100), co psuje testy chcące trafić w
    konkretny, "neutralny" scenariusz. Sinusoida nałożona na trend daje
    realistyczniejszy, umiarkowany RSI przy zachowanym kierunku trendu."""
    base = np.linspace(start, end, n)
    return base + np.sin(np.arange(n) * 1.3) * amplitude


# ---------------- _rsi ----------------

def test_rsi_of_flat_series_is_50():
    # Brak zmian -> brak zysków i strat -> rs = 0/0 = NaN -> fillna(50).
    # Przypadek brzegowy, który łatwo przeoczyć (dzielenie przez zero).
    flat = pd.Series([100.0] * 30)
    assert _rsi(flat, 14).iloc[-1] == 50.0


# ---------------- analyze() - klasyfikacja sygnału ----------------

def test_uptrend_with_clean_pullback_is_good_entry():
    closes = zigzag_trend(100, 130, 42)
    closes = list(closes[:-3]) + [closes[-4] * 0.96, closes[-4] * 0.955, closes[-4] * 0.95]
    result = analyze("TEST", make_history(closes), fifty_two_week_high=200.0, fifty_two_week_low=50.0, cfg=CFG)
    assert result.signal == "GOOD_ENTRY"
    assert result.metrics["uptrend"] is True
    assert result.metrics["pullback_from_recent_high_pct"] >= CFG["min_pullback_from_recent_high_pct"]
    assert result.metrics["sharp_decline"] is False


def test_price_near_52week_high_is_at_top_regardless_of_other_factors():
    closes = list(np.linspace(100, 150, 40))
    result = analyze("TEST", make_history(closes), fifty_two_week_high=150.5, fifty_two_week_low=80.0, cfg=CFG)
    assert result.signal == "AT_TOP"
    assert result.metrics["dist_from_52w_high_pct"] <= CFG["distance_from_high_52w_warning_pct"]


def test_sharp_recent_drop_overrides_oversold_bargain_reading():
    # Spadek 22% w 5 sesji po długim płaskim okresie - RSI samo w sobie
    # wyglądałoby na "wyprzedanie = okazja", ale moduł celowo NIE nagradza
    # świeżego, gwałtownego spadku jak zwykłego wyprzedania (patrz docstring
    # modułu: odróżnienie korekty od szoku fundamentalnego).
    closes = [100.0] * 35 + [100, 95, 88, 82, 78]
    result = analyze("TEST", make_history(closes), fifty_two_week_high=200.0, fifty_two_week_low=50.0, cfg=CFG)
    assert result.signal == "SHARP_DECLINE"
    assert result.metrics["sharp_decline"] is True
    assert result.metrics["recent_change_pct"] <= -CFG["sharp_decline_threshold_pct"]


def test_choppy_downtrend_without_oversold_bounce_is_weak_trend():
    closes = zigzag_trend(130, 100, 45, amplitude=3.0)
    result = analyze("TEST", make_history(list(closes)), fifty_two_week_high=200.0, fifty_two_week_low=50.0, cfg=CFG)
    assert result.signal == "WEAK_TREND"
    assert result.metrics["uptrend"] is False
    assert result.score < 0.5


def test_score_is_clipped_to_0_1_range():
    closes = list(np.linspace(100, 150, 40))
    result = analyze("TEST", make_history(closes), fifty_two_week_high=150.5, fifty_two_week_low=80.0, cfg=CFG)
    assert 0.0 <= result.score <= 1.0


def test_volume_spike_flagged_in_metrics():
    closes = list(np.linspace(100, 105, 30))
    volumes = [1_000_000.0] * 29 + [3_000_000.0]  # 3x ostatniej sesji vs 30-sesyjna średnia
    result = analyze("TEST", make_history(closes, volumes), fifty_two_week_high=200.0, fifty_two_week_low=50.0, cfg=CFG)
    assert result.metrics["volume_spike"] is True


# ---------------- resample_to_weekly ----------------

def test_resample_to_weekly_aggregates_ohlcv_correctly():
    idx = pd.bdate_range("2024-01-01", periods=10)  # dwa pełne tygodnie robocze
    df = pd.DataFrame({
        "Open": range(10), "High": [x + 1 for x in range(10)], "Low": [x - 1 for x in range(10)],
        "Close": range(10), "Volume": [100] * 10,
    }, index=idx)
    weekly = resample_to_weekly(df)
    assert len(weekly) == 2
    first_week = weekly.iloc[0]
    assert first_week["Open"] == 0      # pierwsze Open tygodnia
    assert first_week["High"] == 5      # max High tygodnia (dni 0-4 -> High 1-5)
    assert first_week["Low"] == -1      # min Low tygodnia
    assert first_week["Close"] == 4     # ostatnie Close tygodnia
    assert first_week["Volume"] == 500  # suma wolumenu


# ---------------- analyze_multi_timeframe() - potwierdzenie tygodniowe ----------------

def test_weekly_confirmation_boosts_good_entry_when_aligned():
    closes = zigzag_trend(90, 130, 57, amplitude=2.0)
    closes = list(closes[:-3]) + [closes[-4] * 0.96, closes[-4] * 0.955, closes[-4] * 0.95]
    hist = make_history(closes)
    daily_only = analyze("TEST", hist, 200.0, 50.0, CFG)
    confirmed = analyze_multi_timeframe("TEST", hist, 200.0, 50.0, CFG)

    assert daily_only.signal == "GOOD_ENTRY"  # założenie wejściowe scenariusza
    assert confirmed.signal == "GOOD_ENTRY"
    assert confirmed.score > daily_only.score  # premia za potwierdzenie
    assert confirmed.metrics["weekly_uptrend"] is True


def test_weekly_confirmation_downgrades_good_entry_when_not_aligned():
    # Lokalne odbicie (wygląda jak GOOD_ENTRY na dziennym) w trakcie dłuższego
    # trendu spadkowego na tygodniowym - klasyczna "pułapka byka", którą
    # potwierdzenie tygodniowe ma wyłapać i obniżyć sygnał.
    decline = zigzag_trend(140, 90, 45, amplitude=2.0)
    bounce = np.linspace(decline[-15], decline[-15] + 15, 12)
    closes = list(decline[:-15]) + list(bounce[:-3]) + [bounce[-4] * 0.96, bounce[-4] * 0.955, bounce[-4] * 0.95]
    hist = make_history(closes)
    daily_only = analyze("TEST", hist, 200.0, 50.0, CFG)
    downgraded = analyze_multi_timeframe("TEST", hist, 200.0, 50.0, CFG)

    assert daily_only.signal == "GOOD_ENTRY"  # założenie wejściowe scenariusza
    assert downgraded.signal == "NEUTRAL"
    assert downgraded.score < daily_only.score
    assert downgraded.metrics["weekly_uptrend"] is False


def test_at_top_signal_is_never_touched_by_weekly_confirmation():
    closes = list(np.linspace(90, 150, 60))
    result = analyze_multi_timeframe("TEST", make_history(closes), fifty_two_week_high=150.2, fifty_two_week_low=50.0, cfg=CFG)
    assert result.signal == "AT_TOP"


def test_weekly_confirmation_skipped_when_insufficient_weekly_history():
    closes = list(np.linspace(100, 130, 25))  # ~5 tygodni, za mało dla weekly_ma_long=8
    hist = make_history(closes)
    daily_only = analyze("TEST", hist, 200.0, 50.0, CFG)
    result = analyze_multi_timeframe("TEST", hist, 200.0, 50.0, CFG)
    assert result.signal == daily_only.signal
    assert result.score == daily_only.score
    assert result.metrics["weekly_confirmation_available"] is False


def test_weekly_confirmation_disabled_returns_plain_daily_result():
    cfg_off = {**CFG, "use_weekly_confirmation": False}
    closes = zigzag_trend(90, 130, 57, amplitude=2.0)
    closes = list(closes[:-3]) + [closes[-4] * 0.96, closes[-4] * 0.955, closes[-4] * 0.95]
    hist = make_history(closes)
    daily_only = analyze("TEST", hist, 200.0, 50.0, cfg_off)
    result = analyze_multi_timeframe("TEST", hist, 200.0, 50.0, cfg_off)
    assert result.signal == daily_only.signal
    assert result.score == daily_only.score


# ---------------- compute_relative_strength ----------------

def test_relative_strength_detects_outperformance_vs_benchmark():
    stock = make_history(list(np.linspace(100, 120, 70)))    # +20%
    benchmark = make_history(list(np.linspace(100, 110, 70)))  # +10%
    result = compute_relative_strength(stock, benchmark, lookback_days=60)
    assert result["relative_strength_pct"] > 0
    assert result["stock_return_pct"] > result["benchmark_return_pct"]


def test_relative_strength_none_when_insufficient_history():
    stock = make_history([100.0] * 10)
    benchmark = make_history([100.0] * 10)
    assert compute_relative_strength(stock, benchmark, lookback_days=60) is None
