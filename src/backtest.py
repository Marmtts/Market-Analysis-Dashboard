"""
backtest.py
------------
Samodzielny skrypt: "co by było, gdyby przez ostatnie N lat kupować akcje
zawsze wtedy, gdy nasza logika techniczna dawała sygnał GOOD_ENTRY, i
sprzedawać po ustalonym czasie trzymania (lub przy sygnale AT_TOP)?"

WAŻNE ograniczenia metodologiczne (przeczytaj, zanim uwierzysz w wyniki):
- To backtest WYŁĄCZNIE logiki technicznej (RSI/SMA/Bollinger/52w-high) -
  BEZ komponentu newsowego/sentymentu (bo nie mamy historycznego newsfeedu
  zsynchronizowanego dzień-po-dniu dla całego okresu backtestu za darmo).
  Realne wyniki "na żywo" (technika + newsy) mogą się różnić.
- Brak modelowania poślizgu cenowego (slippage), prowizji, podatku Belki
  i spreadu - w praktyce obniżą one realny wynik.
- "Przeszłe wyniki nie gwarantują przyszłych" - to sformułowanie nie jest tu
  ozdobnikiem, tylko realnym ograniczeniem: 3-5 lat historii jednej spółki to
  wciąż mała próbka statystyczna, mocno zależna od tego, czy okres backtestu
  akurat obejmował hossę, czy bessę.

Użycie:
    python -m src.backtest --ticker MSFT --years 5
    python -m src.backtest --ticker MSFT --years 5 --hold-days 15 --config config.yaml
    python -m src.backtest --check-earnings-window --years 5
"""

from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from rich.console import Console
from rich.table import Table

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.market_data import fetch_history
from src.technical_analysis import _rsi, _bollinger  # reużywamy sprawdzonych wskaźników

console = Console()


@dataclass
class Trade:
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str            # "HOLD_PERIOD" | "AT_TOP_SIGNAL" | "END_OF_DATA"
    return_pct: float = field(init=False)

    def __post_init__(self):
        self.return_pct = (self.exit_price - self.entry_price) / self.entry_price * 100


def _compute_signals(history: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Liczy WEKTOROWO (na całej serii naraz) te same sygnały co
    technical_analysis.analyze(), ale bez zaglądania w przyszłość - wszystkie
    użyte funkcje (rolling/expanding) są z natury przyczynowe (causal)."""
    df = history.copy()
    close = df["Close"]

    df["rsi"] = _rsi(close, cfg["rsi_period"])
    df["ma_short"] = close.rolling(cfg["ma_short"]).mean()
    df["ma_long"] = close.rolling(cfg["ma_long"]).mean()
    df["uptrend"] = df["ma_short"] > df["ma_long"]

    # 52-tygodniowe maksimum liczone jako ROLLING (nie z całego okresu na raz!),
    # żeby nie było "zaglądania w przyszłość" - okno ~252 sesje.
    df["rolling_52w_high"] = close.rolling(252, min_periods=60).max()
    df["dist_from_52w_high_pct"] = (df["rolling_52w_high"] - close) / df["rolling_52w_high"] * 100

    recent_high = close.rolling(20, min_periods=5).max()
    df["pullback_from_recent_high_pct"] = (recent_high - close) / recent_high * 100

    upper_bb, mid_bb, lower_bb = _bollinger(close, cfg["bollinger_period"], cfg["bollinger_std"])
    df["bb_position"] = (close - lower_bb) / (upper_bb - lower_bb)

    at_top = (df["dist_from_52w_high_pct"] <= cfg["distance_from_high_52w_warning_pct"]) | \
             (df["rsi"] >= cfg["rsi_overbought"])

    good_entry = (
        df["uptrend"]
        & (df["pullback_from_recent_high_pct"] >= cfg["min_pullback_from_recent_high_pct"])
        & (~at_top)
        & (df["rsi"] > cfg["rsi_oversold"])   # unikaj łapania "spadającego noża"
    )

    df["signal_at_top"] = at_top
    df["signal_good_entry"] = good_entry
    return df


def run_backtest_on_history(history: pd.DataFrame, hold_days: int, cfg: dict,
                             entries_after: str | None = None,
                             entries_before: str | None = None) -> tuple[list[Trade], pd.DataFrame]:
    """Jak run_backtest(), ale przyjmuje JUŻ POBRANĄ historię cen - pozwala
    to uruchamiać wiele kombinacji parametrów (grid search) na tych samych
    danych bez ponownego odpytywania yfinance za każdym razem.

    entries_after/entries_before (format "YYYY-MM-DD") pozwalają ograniczyć,
    które transakcje SIĘ LICZĄ do wyniku, bez obcinania samej historii -
    wskaźniki (SMA, RSI, rolling 52w) nadal liczą się na PEŁNEJ, przekazanej
    historii (więc nie tracą "rozgrzewki"), tylko wejścia poza podanym oknem
    dat są ignorowane. To jest fundament walidacji walk-forward: pozwala
    ocenić wyniki strategii w okresie "test", nadal używając przyczynowo
    poprawnych wskaźników liczonych od początku dostępnej historii."""
    df = _compute_signals(history, cfg["technical"])

    trades: list[Trade] = []
    in_position = False
    entry_date = entry_price = None
    entry_idx = None

    dates = df.index

    for i in range(len(df)):
        row = df.iloc[i]
        date_str = dates[i].strftime("%Y-%m-%d")

        if not in_position:
            in_window = (entries_after is None or date_str >= entries_after) and \
                        (entries_before is None or date_str < entries_before)
            if in_window and bool(row["signal_good_entry"]):
                # wchodzimy PO CENIE ZAMKNIĘCIA dnia sygnału (uproszczenie - w praktyce
                # realne wejście byłoby raczej na otwarciu następnej sesji)
                in_position = True
                entry_date = dates[i]
                entry_price = float(row["Close"])
                entry_idx = i
        else:
            days_held = i - entry_idx
            hit_top_signal = bool(row["signal_at_top"])
            hit_hold_period = days_held >= hold_days
            is_last_row = i == len(df) - 1

            if hit_top_signal or hit_hold_period or is_last_row:
                exit_reason = (
                    "AT_TOP_SIGNAL" if hit_top_signal else
                    "END_OF_DATA" if is_last_row else
                    "HOLD_PERIOD"
                )
                trades.append(Trade(
                    entry_date=entry_date, entry_price=entry_price,
                    exit_date=dates[i], exit_price=float(row["Close"]),
                    exit_reason=exit_reason,
                ))
                in_position = False

    return trades, df


def run_backtest(ticker: str, years: int, hold_days: int, cfg: dict) -> tuple[list[Trade], pd.DataFrame]:
    period = f"{years}y"
    history = fetch_history(ticker, period=period, interval="1d")
    return run_backtest_on_history(history, hold_days, cfg)


def summarize_backtest(trades: list[Trade], df: pd.DataFrame, ticker: str) -> dict:
    if not trades:
        return {
            "ticker": ticker, "num_trades": 0, "win_rate_pct": None,
            "avg_return_pct": None, "strategy_cumulative_return_pct": None,
            "buy_and_hold_return_pct": None,
        }

    returns = [t.return_pct for t in trades]
    wins = [r for r in returns if r > 0]
    win_rate = len(wins) / len(returns) * 100

    # zwrot skumulowany strategii: zakładamy, że CAŁY kapitał wchodzi w każdą
    # transakcję po kolei (uproszczenie - brak nakładających się pozycji, bo
    # strategia i tak wchodzi w jedną pozycję na raz)
    cumulative = 1.0
    for r in returns:
        cumulative *= (1 + r / 100)
    strategy_cum_return = (cumulative - 1) * 100

    buy_and_hold_return = (float(df["Close"].iloc[-1]) - float(df["Close"].iloc[0])) / float(df["Close"].iloc[0]) * 100

    avg_hold_days = np.mean([(t.exit_date - t.entry_date).days for t in trades])

    return {
        "ticker": ticker,
        "num_trades": len(trades),
        "win_rate_pct": round(win_rate, 1),
        "avg_return_pct": round(float(np.mean(returns)), 2),
        "best_trade_pct": round(max(returns), 2),
        "worst_trade_pct": round(min(returns), 2),
        "avg_hold_days": round(float(avg_hold_days), 1),
        "strategy_cumulative_return_pct": round(strategy_cum_return, 1),
        "buy_and_hold_return_pct": round(buy_and_hold_return, 1),
    }


def print_report(summary: dict, trades: list[Trade], years: int, hold_days: int) -> None:
    console.rule(f"[bold cyan]Backtest: {summary['ticker']} ({years} lat, hold={hold_days} dni)[/bold cyan]")

    if summary["num_trades"] == 0:
        console.print("[yellow]W tym okresie strategia nie wygenerowała ani jednej transakcji "
                       "(zbyt restrykcyjne warunki wejścia lub zbyt krótki okres danych).[/yellow]")
        return

    table = Table(title="Podsumowanie")
    table.add_column("Metryka")
    table.add_column("Wartość")
    table.add_row("Liczba transakcji", str(summary["num_trades"]))
    table.add_row("Win rate", f"{summary['win_rate_pct']}%")
    table.add_row("Średni zwrot / transakcję", f"{summary['avg_return_pct']}%")
    table.add_row("Najlepsza transakcja", f"{summary['best_trade_pct']}%")
    table.add_row("Najgorsza transakcja", f"{summary['worst_trade_pct']}%")
    table.add_row("Średni czas trzymania", f"{summary['avg_hold_days']} dni")
    table.add_row("Zwrot strategii (skumulowany)", f"{summary['strategy_cumulative_return_pct']}%")
    table.add_row("Zwrot Buy & Hold (ten sam okres)", f"{summary['buy_and_hold_return_pct']}%")
    console.print(table)

    diff = summary["strategy_cumulative_return_pct"] - summary["buy_and_hold_return_pct"]
    if diff > 0:
        console.print(f"[green]Strategia pobiła Buy & Hold o {diff:.1f} pkt proc. w tym okresie.[/green]")
    else:
        console.print(f"[yellow]Strategia wypadła gorzej niż Buy & Hold o {abs(diff):.1f} pkt proc. "
                       f"w tym okresie - to się zdarza, szczególnie w silnych, "
                       f"jednokierunkowych trendach (strategia z założenia wychodzi "
                       f"z pozycji, więc traci część ruchu).[/yellow]")

    console.print("\n[dim]Transakcje:[/dim]")
    trade_table = Table()
    trade_table.add_column("Wejście")
    trade_table.add_column("Cena wejścia")
    trade_table.add_column("Wyjście")
    trade_table.add_column("Cena wyjścia")
    trade_table.add_column("Zwrot")
    trade_table.add_column("Powód wyjścia")
    for t in trades:
        color = "green" if t.return_pct > 0 else "red"
        trade_table.add_row(
            t.entry_date.strftime("%Y-%m-%d"), f"{t.entry_price:.2f}",
            t.exit_date.strftime("%Y-%m-%d"), f"{t.exit_price:.2f}",
            f"[{color}]{t.return_pct:+.2f}%[/{color}]", t.exit_reason,
        )
    console.print(trade_table)

    console.print(
        "\n[bold yellow]Zastrzeżenie:[/bold yellow] backtest testuje WYŁĄCZNIE logikę techniczną, "
        "bez komponentu newsowego, bez uwzględnienia prowizji/podatku/poślizgu cenowego. "
        "Wyniki historyczne nie gwarantują przyszłych - traktuj to jako narzędzie do zrozumienia "
        "zachowania strategii, nie jako obietnicę zwrotu."
    )


def run_grid_search(tickers: list[str], years: int, base_cfg: dict,
                     hold_days_options: list[int], rsi_oversold_options: list[int],
                     pullback_options: list[float]) -> list[dict]:
    """Przeszukuje siatkę kombinacji kluczowych parametrów wejścia (hold_days,
    RSI-wyprzedanie, wymagane cofnięcie od szczytu) na WIELU spółkach naraz,
    żeby znaleźć nastawienie, które historycznie dawało najlepszy ŚREDNI
    wynik w watchliście - nie tylko na jednej, przypadkowo dobranej spółce.

    Dane cenowe pobierane są RAZ na spółkę i reużywane dla wszystkich
    kombinacji parametrów - unika to setek zbędnych zapytań do yfinance."""
    histories: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            histories[t] = fetch_history(t, period=f"{years}y", interval="1d")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Pomijam {t} w grid search: {exc}[/yellow]")

    if not histories:
        console.print("[red]Brak jakichkolwiek danych do przeszukania.[/red]")
        return []

    results = []
    total_combos = len(hold_days_options) * len(rsi_oversold_options) * len(pullback_options)
    console.print(f"[dim]Testuję {total_combos} kombinacji parametrów na {len(histories)} spółkach...[/dim]")

    for hold_days in hold_days_options:
        for rsi_oversold in rsi_oversold_options:
            for pullback in pullback_options:
                cfg = copy.deepcopy(base_cfg)
                cfg["technical"]["rsi_oversold"] = rsi_oversold
                cfg["technical"]["min_pullback_from_recent_high_pct"] = pullback

                returns, win_rates, total_trades = [], [], 0
                for ticker, history in histories.items():
                    trades, df = run_backtest_on_history(history, hold_days, cfg)
                    summary = summarize_backtest(trades, df, ticker)
                    if summary["num_trades"] > 0:
                        returns.append(summary["strategy_cumulative_return_pct"])
                        win_rates.append(summary["win_rate_pct"])
                        total_trades += summary["num_trades"]

                if not returns:
                    continue

                results.append({
                    "hold_days": hold_days,
                    "rsi_oversold": rsi_oversold,
                    "min_pullback_pct": pullback,
                    "avg_cum_return_pct": round(sum(returns) / len(returns), 1),
                    "avg_win_rate_pct": round(sum(win_rates) / len(win_rates), 1),
                    "total_trades": total_trades,
                    "tickers_with_trades": len(returns),
                })

    results.sort(key=lambda r: -r["avg_cum_return_pct"])
    return results


def print_grid_search_report(results: list[dict], top_n: int = 15) -> None:
    if not results:
        console.print("[yellow]Żadna kombinacja parametrów nie wygenerowała transakcji.[/yellow]")
        return

    table = Table(title=f"Grid search - top {min(top_n, len(results))} kombinacji wg średniego zwrotu")
    table.add_column("Hold days")
    table.add_column("RSI oversold")
    table.add_column("Min pullback %")
    table.add_column("Śr. zwrot strategii")
    table.add_column("Śr. win rate")
    table.add_column("Transakcje łącznie")
    table.add_column("Spółek z transakcjami")

    for r in results[:top_n]:
        table.add_row(
            str(r["hold_days"]), str(r["rsi_oversold"]), str(r["min_pullback_pct"]),
            f"{r['avg_cum_return_pct']}%", f"{r['avg_win_rate_pct']}%",
            str(r["total_trades"]), str(r["tickers_with_trades"]),
        )
    console.print(table)
    console.print(
        "\n[bold yellow]Zastrzeżenie:[/bold yellow] to nadal backtest WYŁĄCZNIE logiki technicznej "
        "(bez sentymentu i fundamentów), testowany na przeszłych danych. Najlepsza historycznie "
        "kombinacja NIE gwarantuje najlepszych wyników w przyszłości - traktuj to jako punkt wyjścia "
        "do świadomej zmiany config.yaml, nie jako gotową receptę."
    )


def _evaluate_combo(histories: dict[str, pd.DataFrame], hold_days: int, cfg: dict,
                     entries_after: str | None = None, entries_before: str | None = None) -> dict | None:
    """Uśrednia wynik danej kombinacji parametrów po wielu spółkach naraz,
    opcjonalnie ograniczając liczone transakcje do podanego okna dat
    (patrz run_backtest_on_history) - używane zarówno przez zwykły grid
    search, jak i przez walidację walk-forward."""
    returns, win_rates, total_trades = [], [], 0
    for ticker, history in histories.items():
        trades, df = run_backtest_on_history(history, hold_days, cfg,
                                              entries_after=entries_after, entries_before=entries_before)
        summary = summarize_backtest(trades, df, ticker)
        if summary["num_trades"] > 0:
            returns.append(summary["strategy_cumulative_return_pct"])
            win_rates.append(summary["win_rate_pct"])
            total_trades += summary["num_trades"]

    if not returns:
        return None
    return {
        "avg_cum_return_pct": round(sum(returns) / len(returns), 1),
        "avg_win_rate_pct": round(sum(win_rates) / len(win_rates), 1),
        "total_trades": total_trades,
        "tickers_with_trades": len(returns),
    }


def run_walk_forward_validation(tickers: list[str], years: int, test_years: int, base_cfg: dict,
                                 hold_days_options: list[int], rsi_oversold_options: list[int],
                                 pullback_options: list[float], top_n: int = 5) -> dict:
    """Uczciwsza wersja grid search: dzieli historię na okres TRENINGOWY
    (starszy) i TESTOWY (nowszy, nieużywany podczas szukania parametrów).
    Najlepsze kombinacje wg okresu treningowego są NASTĘPNIE oceniane na
    okresie testowym - jeśli wynik się załamuje, to znak przeuczenia się
    pod szum, a nie prawdziwej, powtarzalnej przewagi. Dorzuca też ocenę
    OBECNEGO config.yaml jako punkt odniesienia."""
    histories: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            histories[t] = fetch_history(t, period=f"{years}y", interval="1d")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Pomijam {t}: {exc}[/yellow]")

    if not histories:
        return {"train_ranking": [], "test_results": [], "baseline": None, "split_date": None}

    # Wspólna data podziału (przybliżona - historie różnych spółek mogą się
    # kończyć w nieznacznie różnych dniach sesyjnych, co jest bez znaczenia
    # dla samej metodologii walk-forward).
    last_date = max(h.index[-1] for h in histories.values())
    split_date = (last_date - pd.Timedelta(days=365 * test_years)).strftime("%Y-%m-%d")
    console.print(f"[dim]Podział: trening = dane sprzed {split_date}, test = dane od {split_date} do dziś.[/dim]")

    train_results = []
    for hold_days in hold_days_options:
        for rsi_oversold in rsi_oversold_options:
            for pullback in pullback_options:
                cfg = copy.deepcopy(base_cfg)
                cfg["technical"]["rsi_oversold"] = rsi_oversold
                cfg["technical"]["min_pullback_from_recent_high_pct"] = pullback

                stats = _evaluate_combo(histories, hold_days, cfg, entries_before=split_date)
                if stats:
                    train_results.append({
                        "hold_days": hold_days, "rsi_oversold": rsi_oversold, "min_pullback_pct": pullback,
                        **{f"train_{k}": v for k, v in stats.items()},
                    })

    train_results.sort(key=lambda r: -r["train_avg_cum_return_pct"])
    top_candidates = train_results[:top_n]

    for candidate in top_candidates:
        cfg = copy.deepcopy(base_cfg)
        cfg["technical"]["rsi_oversold"] = candidate["rsi_oversold"]
        cfg["technical"]["min_pullback_from_recent_high_pct"] = candidate["min_pullback_pct"]
        test_stats = _evaluate_combo(histories, candidate["hold_days"], cfg, entries_after=split_date)
        candidate["test"] = test_stats

    # Punkt odniesienia: OBECNE ustawienia z config.yaml (hold_days nie
    # istnieje w configu na żywo - używamy pierwszej wartości z siatki jako
    # rozsądnego, jawnie oznaczonego przybliżenia).
    baseline_cfg = copy.deepcopy(base_cfg)
    baseline_hold_days = hold_days_options[len(hold_days_options) // 2]
    baseline_train = _evaluate_combo(histories, baseline_hold_days, baseline_cfg, entries_before=split_date)
    baseline_test = _evaluate_combo(histories, baseline_hold_days, baseline_cfg, entries_after=split_date)

    return {
        "split_date": split_date,
        "top_candidates": top_candidates,
        "baseline": {
            "rsi_oversold": base_cfg["technical"]["rsi_oversold"],
            "min_pullback_pct": base_cfg["technical"]["min_pullback_from_recent_high_pct"],
            "hold_days_used": baseline_hold_days,
            "train": baseline_train,
            "test": baseline_test,
        },
    }


def print_walk_forward_report(result: dict) -> None:
    if not result["top_candidates"]:
        console.print("[yellow]Brak wyników - sprawdź dostępność danych dla podanych tickerów.[/yellow]")
        return

    table = Table(title=f"Walk-forward: trening vs test (podział: {result['split_date']})")
    table.add_column("Hold/RSI/Pullback")
    table.add_column("Trening: zwrot")
    table.add_column("Trening: win rate")
    table.add_column("Test: zwrot")
    table.add_column("Test: win rate")
    table.add_column("Różnica (przeuczenie?)")

    for c in result["top_candidates"]:
        params = f"{c['hold_days']}d / RSI{c['rsi_oversold']} / {c['min_pullback_pct']}%"
        test = c.get("test")
        if test:
            gap = c["train_avg_cum_return_pct"] - test["avg_cum_return_pct"]
            gap_str = f"{gap:+.1f} pkt"
            test_return = f"{test['avg_cum_return_pct']}%"
            test_win = f"{test['avg_win_rate_pct']}%"
        else:
            gap_str, test_return, test_win = "brak transakcji w teście", "—", "—"
        table.add_row(params, f"{c['train_avg_cum_return_pct']}%", f"{c['train_avg_win_rate_pct']}%",
                       test_return, test_win, gap_str)

    console.print(table)

    b = result["baseline"]
    console.print(f"\n[bold]Punkt odniesienia - OBECNY config.yaml[/bold] "
                   f"(RSI{b['rsi_oversold']} / {b['min_pullback_pct']}%, hold_days~{b['hold_days_used']} "
                   f"jako przybliżenie, bo hold_days nie istnieje w configu na żywo):")
    if b["train"] and b["test"]:
        console.print(f"  Trening: {b['train']['avg_cum_return_pct']}% (win rate {b['train']['avg_win_rate_pct']}%) | "
                       f"Test: {b['test']['avg_cum_return_pct']}% (win rate {b['test']['avg_win_rate_pct']}%)")
    else:
        console.print("  [yellow]Brak wystarczających transakcji do oceny baseline.[/yellow]")

    console.print(
        "\n[bold yellow]Jak czytać ten raport:[/bold yellow]\n"
        "- Kolumna 'Różnica' pokazuje, o ile zwrot w TESTOWYM (nieużytym do optymalizacji) okresie jest "
        "niższy niż w treningowym. Duża, dodatnia różnica = silne przeuczenie się pod szum historyczny - "
        "taka kombinacja może wyglądać świetnie w backteście, ale zawieść na żywo.\n"
        "- Szukaj kombinacji z WYSOKIM zwrotem w teście, a NIE tylko wysokim zwrotem w treningu.\n"
        "- Porównaj wynik testowy kandydatów z wynikiem testowym OBECNEGO config.yaml (baseline) - zmiana "
        "ma sens tylko jeśli realnie bije baseline NA DANYCH, KTÓRYCH NIE UŻYTO do jej znalezienia."
    )


def fetch_historical_earnings_dates(ticker: str, limit: int = 40) -> list[date]:
    """Pobiera historyczne (i najbliższe przyszłe) daty publikacji wyników
    kwartalnych z Yahoo Finance (`Ticker.get_earnings_dates`) - w
    przeciwieństwie do `fundamentals.fetch_next_earnings_date` (tylko
    najbliższy termin), tu potrzebujemy całej historii do sprawdzenia, czy
    dawne wejścia GOOD_ENTRY wypadały w oknie przedwynikowym. `limit`
    ogranicza liczbę zwracanych kwartałów (Yahoo zwraca też kilka przyszłych
    - te i tak nie mają wpływu na historyczne transakcje, są nieszkodliwe)."""
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).get_earnings_dates(limit=limit)
        if raw is None or raw.empty:
            return []
        return sorted({idx.date() for idx in raw.index})
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Brak historycznych dat wyników dla {ticker}: {exc}[/yellow]")
        return []


def _is_pre_earnings_entry(entry_date: pd.Timestamp, earnings_dates: list[date], warning_days: int) -> bool:
    """Czy dzień wejścia w pozycję wypada <= warning_days dni PRZED
    najbliższą kolejną (od dnia wejścia) publikacją wyników."""
    entry_d = entry_date.date() if hasattr(entry_date, "date") else entry_date
    upcoming = [d for d in earnings_dates if d >= entry_d]
    if not upcoming:
        return False
    return (min(upcoming) - entry_d).days <= warning_days


def run_earnings_window_analysis(tickers: list[str], years: int, hold_days: int, base_cfg: dict,
                                  warning_days: int) -> dict:
    """Sprawdza hipotezę z README ("czy okno tuż przed wynikami kwartalnymi
    pogarsza sygnały GOOD_ENTRY") - dzieli WSZYSTKIE transakcje strategii
    technicznej (na całej watchliście, ten sam backtest co zwykły grid
    search) na dwie grupy wg tego, czy dzień wejścia wypadł w oknie
    przedwynikowym, i porównuje ich średni zwrot i win rate."""
    histories: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            histories[t] = fetch_history(t, period=f"{years}y", interval="1d")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Pomijam {t}: {exc}[/yellow]")

    pre_returns: list[float] = []
    other_returns: list[float] = []
    per_ticker_rows: list[dict] = []

    for ticker, history in histories.items():
        trades, _ = run_backtest_on_history(history, hold_days, base_cfg)
        if not trades:
            continue
        earnings_dates = fetch_historical_earnings_dates(ticker, limit=years * 4 + 8)
        if not earnings_dates:
            continue

        t_pre = [t.return_pct for t in trades if _is_pre_earnings_entry(t.entry_date, earnings_dates, warning_days)]
        t_other = [t.return_pct for t in trades if not _is_pre_earnings_entry(t.entry_date, earnings_dates, warning_days)]
        pre_returns.extend(t_pre)
        other_returns.extend(t_other)
        if t_pre or t_other:
            per_ticker_rows.append({
                "ticker": ticker,
                "pre_earnings_trades": len(t_pre),
                "pre_earnings_avg_return_pct": round(sum(t_pre) / len(t_pre), 2) if t_pre else None,
                "other_trades": len(t_other),
                "other_avg_return_pct": round(sum(t_other) / len(t_other), 2) if t_other else None,
            })

    def _summary(returns: list[float]) -> dict | None:
        if not returns:
            return None
        wins = [r for r in returns if r > 0]
        return {
            "num_trades": len(returns),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
            "win_rate_pct": round(len(wins) / len(returns) * 100, 1),
        }

    return {
        "warning_days": warning_days,
        "pre_earnings": _summary(pre_returns),
        "other": _summary(other_returns),
        "per_ticker": per_ticker_rows,
    }


def print_earnings_window_report(result: dict) -> None:
    pre, other = result["pre_earnings"], result["other"]
    console.rule(f"[bold cyan]Okno przedwynikowe ({result['warning_days']} dni) a jakość sygnałów GOOD_ENTRY[/bold cyan]")

    if not pre or not other:
        console.print("[yellow]Za mało transakcji w jednej z grup (przedwynikowej lub pozostałej), "
                       "by cokolwiek porównać - zwiększ --years albo watchlistę.[/yellow]")
        return

    table = Table(title="Transakcje wchodzone w oknie przedwynikowym vs pozostałe")
    table.add_column("Grupa")
    table.add_column("Liczba transakcji")
    table.add_column("Śr. zwrot")
    table.add_column("Win rate")
    table.add_row(f"W oknie <= {result['warning_days']} dni przed wynikami",
                   str(pre["num_trades"]), f"{pre['avg_return_pct']}%", f"{pre['win_rate_pct']}%")
    table.add_row("Poza oknem przedwynikowym",
                   str(other["num_trades"]), f"{other['avg_return_pct']}%", f"{other['win_rate_pct']}%")
    console.print(table)

    return_gap = pre["avg_return_pct"] - other["avg_return_pct"]
    win_rate_gap = pre["win_rate_pct"] - other["win_rate_pct"]
    if return_gap < -0.5 or win_rate_gap < -5:
        console.print(
            f"[yellow]Sygnał: wejścia tuż przed wynikami wypadają gorzej "
            f"({return_gap:+.2f} pkt proc. śr. zwrotu, {win_rate_gap:+.1f} pkt win rate) - "
            f"warto rozważyć obniżanie kategorii GOOD_ENTRY w tym oknie, nie tylko informacyjne "
            f"ostrzeżenie jak obecnie.[/yellow]"
        )
    else:
        console.print(
            f"[green]Brak wyraźnego pogorszenia w oknie przedwynikowym "
            f"({return_gap:+.2f} pkt proc. śr. zwrotu, {win_rate_gap:+.1f} pkt win rate) - obecne, "
            f"czysto informacyjne ostrzeżenie (bez zmiany kategorii) wydaje się uzasadnione.[/green]"
        )

    if result["per_ticker"]:
        detail = Table(title="Szczegóły per spółka")
        detail.add_column("Ticker")
        detail.add_column("Transakcje przedwynikowe")
        detail.add_column("Śr. zwrot")
        detail.add_column("Pozostałe transakcje")
        detail.add_column("Śr. zwrot")
        for row in result["per_ticker"]:
            detail.add_row(
                row["ticker"],
                str(row["pre_earnings_trades"]),
                f"{row['pre_earnings_avg_return_pct']}%" if row["pre_earnings_avg_return_pct"] is not None else "—",
                str(row["other_trades"]),
                f"{row['other_avg_return_pct']}%" if row["other_avg_return_pct"] is not None else "—",
            )
        console.print(detail)

    console.print(
        "\n[bold yellow]Zastrzeżenie:[/bold yellow] daty wyników z Yahoo Finance bywają skorygowane "
        "wstecz względem tego, co było znane 'na żywo' w danym dniu (możliwy niewielki wyciek "
        "przyszłej informacji), a liczba transakcji w oknie przedwynikowym jest z natury mała "
        "(kilka dni na kwartał na spółkę) - traktuj to jako wstępny sygnał kierunkowy, nie "
        "ostateczny dowód."
    )


def main():
    parser = argparse.ArgumentParser(description="Backtest strategii wejścia XTB Trend Watch")
    parser.add_argument("--ticker", help="Ticker Yahoo Finance, np. MSFT, ALE.WA (pojedynczy backtest)")
    parser.add_argument("--years", type=int, default=5, help="Ile lat historii testować (domyślnie 5)")
    parser.add_argument("--hold-days", type=int, default=20,
                         help="Maks. liczba dni trzymania pozycji, jeśli nie pojawi się sygnał AT_TOP (domyślnie 20)")
    parser.add_argument("--config", default="config.yaml", help="Ścieżka do pliku konfiguracyjnego")
    parser.add_argument("--grid-search", action="store_true",
                         help="Zamiast pojedynczego backtestu, przeszukaj siatkę parametrów na wielu spółkach")
    parser.add_argument("--tickers", default=None,
                         help="Lista tickerów oddzielonych przecinkami do grid search (domyślnie: watchlist z config.yaml)")
    parser.add_argument("--hold-days-grid", default="10,20,30",
                         help="Wartości hold-days do przetestowania w grid search (domyślnie 10,20,30)")
    parser.add_argument("--rsi-oversold-grid", default="25,30,35,40",
                         help="Wartości RSI-wyprzedania do przetestowania (domyślnie 25,30,35,40)")
    parser.add_argument("--pullback-grid", default="2,3,5,8",
                         help="Wartości min. cofnięcia od szczytu %% do przetestowania (domyślnie 2,3,5,8)")
    parser.add_argument("--walk-forward", action="store_true",
                         help="Zamiast zwykłego grid search, zwaliduj najlepsze kombinacje na OSOBNYM, "
                              "nieużytym do optymalizacji okresie testowym (ochrona przed przeuczeniem)")
    parser.add_argument("--test-years", type=int, default=2,
                         help="Ile ostatnich lat odłożyć jako okres TESTOWY w --walk-forward (domyślnie 2)")
    parser.add_argument("--check-earnings-window", action="store_true",
                         help="Sprawdź, czy wejścia GOOD_ENTRY tuż przed wynikami kwartalnymi wypadają "
                              "gorzej niż pozostałe (na watchliście/--tickers, wg OBECNEGO config.yaml)")
    parser.add_argument("--earnings-warning-days", type=int, default=None,
                         help="Szerokość okna przedwynikowego w dniach (domyślnie: fundamentals."
                              "earnings_warning_days z configu, albo 7)")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if args.walk_forward:
        tickers = (args.tickers.split(",") if args.tickers
                   else [c["ticker"] for c in cfg.get("watchlist", [])])
        tickers = [t.strip() for t in tickers if t.strip()]
        if not tickers:
            console.print("[red]Brak tickerów (pusta watchlista i brak --tickers).[/red]")
            return
        if args.years <= args.test_years:
            console.print(f"[red]--years ({args.years}) musi być większe niż --test-years ({args.test_years}).[/red]")
            return

        result = run_walk_forward_validation(
            tickers=tickers, years=args.years, test_years=args.test_years, base_cfg=cfg,
            hold_days_options=[int(x) for x in args.hold_days_grid.split(",")],
            rsi_oversold_options=[int(x) for x in args.rsi_oversold_grid.split(",")],
            pullback_options=[float(x) for x in args.pullback_grid.split(",")],
        )
        print_walk_forward_report(result)
        return

    if args.check_earnings_window:
        tickers = (args.tickers.split(",") if args.tickers
                   else [c["ticker"] for c in cfg.get("watchlist", [])])
        tickers = [t.strip() for t in tickers if t.strip()]
        if not tickers:
            console.print("[red]Brak tickerów do przetestowania (pusta watchlista i brak --tickers).[/red]")
            return

        warning_days = args.earnings_warning_days
        if warning_days is None:
            warning_days = cfg.get("fundamentals", {}).get("earnings_warning_days", 7)

        result = run_earnings_window_analysis(
            tickers=tickers, years=args.years, hold_days=args.hold_days,
            base_cfg=cfg, warning_days=warning_days,
        )
        print_earnings_window_report(result)
        return

    if args.grid_search:
        tickers = (args.tickers.split(",") if args.tickers
                   else [c["ticker"] for c in cfg.get("watchlist", [])])
        tickers = [t.strip() for t in tickers if t.strip()]
        if not tickers:
            console.print("[red]Brak tickerów do przetestowania (pusta watchlista i brak --tickers).[/red]")
            return

        results = run_grid_search(
            tickers=tickers, years=args.years, base_cfg=cfg,
            hold_days_options=[int(x) for x in args.hold_days_grid.split(",")],
            rsi_oversold_options=[int(x) for x in args.rsi_oversold_grid.split(",")],
            pullback_options=[float(x) for x in args.pullback_grid.split(",")],
        )
        print_grid_search_report(results)
        return

    if not args.ticker:
        console.print("[red]Podaj --ticker (pojedynczy backtest) albo --grid-search (przeszukiwanie parametrów).[/red]")
        return

    trades, df = run_backtest(args.ticker, args.years, args.hold_days, cfg)
    summary = summarize_backtest(trades, df, args.ticker)
    print_report(summary, trades, args.years, args.hold_days)


if __name__ == "__main__":
    main()
