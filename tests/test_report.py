import pytest

from src.report import combine_scores, compute_max_drawdown
from src.technical_analysis import TechnicalResult
from src.llm_sentiment import SentimentResult, SentimentTrendResult
from src.fundamentals import FundamentalResult

SCORING_CFG = {
    "technical_weight": 0.55,
    "sentiment_weight": 0.30,
    "fundamentals_weight": 0.15,
    "suggestion_threshold": 0.55,
    "hard_block_on_top": True,
    "hard_block_on_sharp_decline": True,
}


def technical(signal="NEUTRAL", score=0.6):
    return TechnicalResult(ticker="TEST", signal=signal, score=score)


def sentiment(score=0.0):
    return SentimentResult(ticker="TEST", score=score, summary="", method="lexicon")


def fundamentals(available=True, score=0.6):
    return FundamentalResult(ticker="TEST", available=available, score=score)


# ---------------- Hard block - zasada bezpieczeństwa z docstringa modułu:
# AT_TOP/SHARP_DECLINE nigdy nie trafiają do sekcji "kupuj", niezależnie od
# tego, jak wysoki byłby final_score. ----------------

def test_at_top_is_hard_blocked_regardless_of_score():
    result = combine_scores(technical(signal="AT_TOP", score=1.0), sentiment(1.0), SCORING_CFG,
                             fundamentals=fundamentals(score=1.0))
    assert result["hard_blocked"] is True
    assert result["category"] == "UNIKAJ - blisko szczytu trendu"


def test_sharp_decline_is_hard_blocked_regardless_of_score():
    result = combine_scores(technical(signal="SHARP_DECLINE", score=1.0), sentiment(1.0), SCORING_CFG,
                             fundamentals=fundamentals(score=1.0))
    assert result["hard_blocked"] is True
    assert result["category"] == "UNIKAJ - świeży gwałtowny spadek (sprawdź przyczynę)"


def test_hard_block_can_be_disabled_via_config():
    cfg = {**SCORING_CFG, "hard_block_on_top": False}
    result = combine_scores(technical(signal="AT_TOP", score=1.0), sentiment(1.0), cfg,
                             fundamentals=fundamentals(score=1.0))
    assert result["hard_blocked"] is False
    assert result["category"] != "UNIKAJ - blisko szczytu trendu"


# ---------------- Kategorie wg progu i sygnału ----------------

def test_good_entry_above_threshold_gets_entry_point_category():
    result = combine_scores(technical(signal="GOOD_ENTRY", score=0.9), sentiment(0.5), SCORING_CFG,
                             fundamentals=fundamentals(score=0.8))
    assert result["category"] == "WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia"


def test_above_threshold_without_good_entry_signal_gets_neutral_positive():
    result = combine_scores(technical(signal="NEUTRAL", score=0.9), sentiment(0.5), SCORING_CFG,
                             fundamentals=fundamentals(score=0.8))
    assert result["category"] == "NEUTRALNIE POZYTYWNIE - brak jednoznacznego sygnału wejścia"


def test_below_threshold_gets_no_signal_category():
    result = combine_scores(technical(signal="NEUTRAL", score=0.1), sentiment(-0.5), SCORING_CFG,
                             fundamentals=fundamentals(score=0.1))
    assert result["category"] == "BRAK SYGNAŁU / POCZEKAJ"


# ---------------- Renormalizacja wag, gdy fundamenty niedostępne ----------------

def test_missing_fundamentals_renormalizes_weights_to_technical_and_sentiment_only():
    # Bez fundamentów: effective weights = 0.55/0.85 tech, 0.30/0.85 sentiment.
    # sentiment.score=0 -> sentiment_norm=0.5 (normalizacja [-1,1] -> [0,1]).
    result = combine_scores(technical(score=0.8), sentiment(0.0), SCORING_CFG,
                             fundamentals=fundamentals(available=False))
    tech_w = 0.55 / 0.85
    sent_w = 0.30 / 0.85
    expected = round(0.8 * tech_w + 0.5 * sent_w, 3)
    assert result["final_score"] == expected
    assert result["fundamentals_used"] is False


def test_fundamentals_none_is_treated_as_unavailable():
    result = combine_scores(technical(score=0.8), sentiment(0.0), SCORING_CFG, fundamentals=None)
    assert result["fundamentals_used"] is False


def test_available_fundamentals_are_included_in_final_score():
    with_fund = combine_scores(technical(score=0.6), sentiment(0.0), SCORING_CFG,
                                fundamentals=fundamentals(available=True, score=1.0))
    without_fund = combine_scores(technical(score=0.6), sentiment(0.0), SCORING_CFG,
                                   fundamentals=fundamentals(available=False, score=1.0))
    assert with_fund["fundamentals_used"] is True
    # Wysoki score fundamentów powinien podciągnąć wynik w górę względem
    # wariantu bez fundamentów (ta sama technika/sentyment).
    assert with_fund["final_score"] > without_fund["final_score"]


# ---------------- Korekta od trendu sentymentu (+/- 0.05, przycięta do [0,1]) ----------------

def test_improving_trend_adds_positive_adjustment():
    trend = SentimentTrendResult(ticker="TEST", monthly=[], trend_direction="POPRAWIA_SIE", trend_score_delta=0.2, llm_narrative="")
    result = combine_scores(technical(score=0.5), sentiment(0.0), SCORING_CFG, trend=trend,
                             fundamentals=fundamentals(score=0.5))
    assert result["trend_adjustment"] == 0.05


def test_worsening_trend_adds_negative_adjustment():
    trend = SentimentTrendResult(ticker="TEST", monthly=[], trend_direction="POGARSZA_SIE", trend_score_delta=-0.2, llm_narrative="")
    result = combine_scores(technical(score=0.5), sentiment(0.0), SCORING_CFG, trend=trend,
                             fundamentals=fundamentals(score=0.5))
    assert result["trend_adjustment"] == -0.05


def test_final_score_is_clamped_to_one_even_with_trend_bonus():
    trend = SentimentTrendResult(ticker="TEST", monthly=[], trend_direction="POPRAWIA_SIE", trend_score_delta=0.2, llm_narrative="")
    result = combine_scores(technical(score=1.0), sentiment(1.0), SCORING_CFG, trend=trend,
                             fundamentals=fundamentals(score=1.0))
    assert result["final_score"] == 1.0


# ---------------- Próg efektywny - podnoszony w dniach podwyższonego ryzyka makro ----------------

def test_threshold_adjustment_raises_effective_threshold():
    result = combine_scores(technical(score=0.5), sentiment(0.0), SCORING_CFG, threshold_adjustment=0.1)
    assert result["effective_threshold"] == pytest.approx(0.65)


# =====================================================================
# compute_max_drawdown
# =====================================================================

def test_drawdown_unavailable_with_fewer_than_two_points():
    assert compute_max_drawdown([]) == {"available": False}
    assert compute_max_drawdown([{"total_value": 100, "ts": "2026-01-01"}]) == {"available": False}


def test_drawdown_zero_when_curve_only_rises():
    points = [{"total_value": v, "ts": f"2026-01-0{i+1}"} for i, v in enumerate([100, 110, 120, 130])]
    result = compute_max_drawdown(points)
    assert result["max_drawdown_pct"] == 0.0
    assert result["current_drawdown_pct"] == 0.0


def test_drawdown_picks_the_worst_dip_not_the_last_one():
    # Szczyt 100 -> dołek 50 (dd 50%) -> szczyt 200 -> dołek 160 (dd 20%).
    # Najgorszy dołek to PIERWSZY (50%), mimo że ostatni jest bliżej końca.
    points = [
        {"total_value": 100, "ts": "d1"},
        {"total_value": 50, "ts": "d2"},
        {"total_value": 200, "ts": "d3"},
        {"total_value": 160, "ts": "d4"},
    ]
    result = compute_max_drawdown(points)
    assert result["max_drawdown_pct"] == 50.0
    assert result["max_drawdown_peak_date"] == "d1"
    assert result["max_drawdown_trough_date"] == "d2"


def test_current_drawdown_reflects_recovery_separately_from_max():
    # Szczyt 100 -> dołek 40 (dd 60%, to max) -> odbicie do 90 (current dd = 10%).
    points = [
        {"total_value": 100, "ts": "d1"},
        {"total_value": 40, "ts": "d2"},
        {"total_value": 90, "ts": "d3"},
    ]
    result = compute_max_drawdown(points)
    assert result["max_drawdown_pct"] == 60.0
    assert result["current_drawdown_pct"] == 10.0
