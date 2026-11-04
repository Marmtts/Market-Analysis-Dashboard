from datetime import datetime, timedelta

import pytest

from src.report import combine_scores, compute_max_drawdown, compute_signal_discipline, compute_dividend_forecast
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


# ---------------- compute_signal_discipline - "czy trzymasz się własnego
# sygnału": konfrontuje każdy zakup z werdyktem narzędzia sprzed tej decyzji
# (position["signal_at_buy"], dogrywane przez db.get_score_near_date zanim
# pozycje trafią do tej funkcji - sama funkcja jest czystą klasyfikacją). ----

def _position(ticker="TEST", buy_date="2024-06-01", status="open",
              signal_category=None, realized_pl_pct=None, unrealized_pct=None,
              shares=10.0, buy_price=100.0):
    p = {"ticker": ticker, "buy_date": buy_date, "status": status, "shares": shares, "buy_price": buy_price}
    p["signal_at_buy"] = {"category": signal_category, "ts": buy_date} if signal_category else None
    if realized_pl_pct is not None:
        p["realized_pl_pct"] = realized_pl_pct
    if unrealized_pct is not None:
        p["unrealized_pct"] = unrealized_pct
    return p


def test_buy_after_good_entry_verdict_is_zgodnie():
    result = compute_signal_discipline([_position(signal_category="WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia")])
    assert result["zgodnie"]["count"] == 1
    assert result["wbrew"]["count"] == 0


def test_buy_despite_avoid_verdict_is_wbrew():
    result = compute_signal_discipline([_position(signal_category="UNIKAJ - blisko szczytu trendu")])
    assert result["wbrew"]["count"] == 1


def test_buy_with_neutral_verdict_is_neutralnie():
    result = compute_signal_discipline([_position(signal_category="NEUTRALNIE POZYTYWNIE - brak jednoznacznego sygnału wejścia")])
    assert result["neutralnie"]["count"] == 1


def test_buy_without_verdict_in_window_is_brak_danych():
    result = compute_signal_discipline([_position(signal_category=None)])
    assert result["brak_danych"]["count"] == 1


def test_avg_realized_pct_only_counts_closed_positions():
    positions = [
        _position(ticker="A", status="closed", signal_category="UNIKAJ - blisko szczytu trendu", realized_pl_pct=-10.0),
        _position(ticker="B", status="closed", signal_category="UNIKAJ - blisko szczytu trendu", realized_pl_pct=-30.0),
        _position(ticker="C", status="open", signal_category="UNIKAJ - blisko szczytu trendu", unrealized_pct=50.0),
    ]
    result = compute_signal_discipline(positions)
    assert result["wbrew"]["count"] == 3
    assert result["wbrew"]["closed_count"] == 2
    # Średnia TYLKO z A i B (-10, -30) -> -20 - otwarta pozycja C (mimo
    # dobrego unrealized_pct) nie powinna zaciągać średniej w górę, bo jej
    # wynik jeszcze się nie zrealizował.
    assert result["wbrew"]["avg_realized_pl_pct"] == -20.0


def test_avg_realized_pct_is_none_without_any_closed_position():
    result = compute_signal_discipline([_position(signal_category="WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia")])
    assert result["zgodnie"]["avg_realized_pl_pct"] is None


def test_avg_is_weighted_by_cost_basis_not_a_simple_average():
    # Regresja na realny przypadek: pozycja z błędnym/groszowym buy_price z
    # importu (np. 0.001) dająca pozorne +3 000 000% potrafiła zdominować
    # zwykłą średnią arytmetyczną, mimo że realnie zaangażowany kapitał był
    # pomijalny (0.26 akcji po 0.001 = 0.00026 zł). Ważenie kosztem pozycji
    # sprowadza jej wpływ do ~zera, zamiast psuć całą statystykę.
    positions = [
        _position(ticker="NORMALNA", status="closed", signal_category="UNIKAJ - blisko szczytu trendu",
                  realized_pl_pct=-10.0, shares=10.0, buy_price=100.0),   # koszt 1000
        _position(ticker="GROSZOWA", status="closed", signal_category="UNIKAJ - blisko szczytu trendu",
                  realized_pl_pct=3_317_900.0, shares=0.2614, buy_price=0.001),  # koszt ~0.00026
    ]
    result = compute_signal_discipline(positions)
    # Wynik zdominowany przez pozycję z realnym kapitałem (ok. -9.13%, blisko
    # jej -10%) - zwykła średnia arytmetyczna dałaby tu ~1 658 945% (średnia
    # z -10 i 3 317 900).
    assert result["wbrew"]["avg_realized_pl_pct"] == pytest.approx(-9.13, abs=0.01)


# ---------------- compute_dividend_forecast - ekstrapolacja dywidend z
# ostatnich 12 mies. na kolejne 12 mies., tylko dla obecnie posiadanych
# spółek. Waluta dywidendy == base_currency w testach, żeby ominąć
# zapytania o kurs (rate=1.0 bez I/O, patrz _convert_dividend_to_base_currency). ----

def _dividend(ticker, days_ago, amount=100.0, currency="PLN"):
    pay_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {"ticker": ticker, "currency": currency, "pay_date": pay_date, "amount_gross": amount, "withholding_tax": 0.0}


def test_dividend_within_last_12_months_for_held_ticker_is_projected():
    result = compute_dividend_forecast([_dividend("MSFT", days_ago=100)], held_tickers={"MSFT"}, base_currency="PLN")
    assert result["total_projected"] == 100.0
    assert result["by_ticker"]["MSFT"]["payments_last_12mo"] == 1


def test_dividend_older_than_12_months_is_excluded():
    result = compute_dividend_forecast([_dividend("MSFT", days_ago=400)], held_tickers={"MSFT"}, base_currency="PLN")
    assert result["total_projected"] == 0.0
    assert "MSFT" not in result["by_ticker"]


def test_dividend_for_no_longer_held_ticker_is_excluded():
    # Sprzedana pozycja - nie ma podstaw zakładać przyszłej wypłaty.
    result = compute_dividend_forecast([_dividend("MSFT", days_ago=30)], held_tickers={"AAPL"}, base_currency="PLN")
    assert result["total_projected"] == 0.0


def test_multiple_dividends_same_ticker_sum_and_count_payments():
    dividends = [_dividend("MSFT", days_ago=30, amount=50.0), _dividend("MSFT", days_ago=120, amount=60.0)]
    result = compute_dividend_forecast(dividends, held_tickers={"MSFT"}, base_currency="PLN")
    assert result["total_projected"] == 110.0
    assert result["by_ticker"]["MSFT"]["payments_last_12mo"] == 2


def test_empty_inputs_return_zero_forecast():
    assert compute_dividend_forecast([], held_tickers={"MSFT"})["total_projected"] == 0.0
    assert compute_dividend_forecast([_dividend("MSFT", days_ago=10)], held_tickers=set())["total_projected"] == 0.0
