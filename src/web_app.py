"""
web_app.py
------------
Dashboard webowy XTB Trend Watch. Uruchom:

    python -m src.web_app --config config.yaml

Potem otwórz w przeglądarce: http://localhost:8000

Co robi w tle:
- Co `web.refresh_interval_minutes` minut (domyślnie 60) automatycznie
  uruchamia pełen cykl analizy (src/analysis_engine.run_full_analysis) dla
  aktualnej watchlisty z bazy danych.
- Postęp analizy w czasie rzeczywistym oraz finalne wyniki są wypychane do
  WSZYSTKICH podłączonych przeglądarek przez WebSocket (/ws).
- Wyniki są też cache'owane w SQLite, więc świeżo otwarta karta przeglądarki
  od razu widzi ostatnie dostępne dane, zanim skończy się kolejny cykl.

Endpointy REST:
- GET  /api/watchlist                - lista obserwowanych spółek
- POST /api/watchlist                - dodaj spółkę {ticker, name, xtb_symbol?}
- DELETE /api/watchlist/{ticker}     - usuń spółkę
- GET  /api/results                  - ostatnie wyniki analizy (cache)
- GET  /api/chart/{ticker}           - dane OHLC do wykresu świecowego
- GET  /api/logs                     - ostatnie logi
- POST /api/run-now                  - wymuś natychmiastową analizę (w tle)
- WS   /ws                            - kanał live-update
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.append(str(Path(__file__).resolve().parent.parent))

import yfinance as yf

from src import db
from src.analysis_engine import run_full_analysis, analyze_company
from src.market_data import fetch_history, fetch_benchmark_history, get_benchmark_fallbacks, BenchmarkUnavailable
from src.json_utils import sanitize_for_json
from src.report import (
    evaluate_portfolio_position, compute_max_drawdown,
    compute_portfolio_risk_summary, compute_portfolio_sector_exposure,
    compute_tax_summary, summarize_portfolio_by_currency,
    compute_portfolio_statistics, compute_benchmark_comparison,
    build_closed_trades_export_rows,
    compute_twr_curve, prepare_cash_flows_for_currency,
)
from src.daily_brief import generate_daily_brief
from src.chatbot import answer_chat_question
from src.xtb_import import parse_xtb_report
from src.fx_rates import get_fx_rate
from src.notifications import notify_price_alerts, send_discord_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("xtb_trend_watch.web_app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

_cfg: dict = {}
_is_running: bool = False
_next_run_at: datetime | None = None
# Ostatnie ręczne odświeżenie per ticker (timestamp) - chroni yfinance/Finnhub
# przed zbyt częstymi zapytaniami o tę samą spółkę (patrz manual_refresh_cooldown_seconds).
_last_manual_refresh: dict[str, float] = {}
# Zapamiętuje ostatni WYSŁANY typ alertu cenowego per pozycja portfela -
# zapobiega spamowaniu tym samym powiadomieniem co kilka minut. Klucz:
# (position_id, alert_type) -> True. Czyszczone, gdy sytuacja przestaje
# spełniać warunek alertu (patrz _check_price_alerts).
_sent_price_alerts: set[tuple[int, str]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _broadcast_loop
    _broadcast_loop = asyncio.get_event_loop()
    db.init_db(seed_watchlist=_cfg.get("watchlist", []))
    task = asyncio.create_task(_background_scheduler())
    price_alert_task = asyncio.create_task(_price_alert_scheduler())
    logger.info("XTB Trend Watch Dashboard wystartował.")
    yield
    task.cancel()
    price_alert_task.cancel()


app = FastAPI(title="XTB Trend Watch Dashboard", lifespan=lifespan)


# =====================================================================
# WebSocket - broadcast do wszystkich podłączonych przeglądarek
# =====================================================================

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
_broadcast_loop: asyncio.AbstractEventLoop | None = None


def _sync_progress_to_broadcast(message: str, level: str = "info") -> None:
    """Callback wywoływany SYNCHRONICZNIE z wątku roboczego (analiza jest
    blokująca - yfinance/requests) - bezpiecznie planuje broadcast na pętli
    asyncio serwera."""
    db.log_event(level, message)
    if _broadcast_loop is not None:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type": "progress", "level": level, "message": message}),
            _broadcast_loop,
        )


# =====================================================================
# Logika uruchamiania analizy w tle (w osobnym wątku, żeby nie blokować
# pętli asyncio - yfinance/requests/Ollama to wywołania synchroniczne)
# =====================================================================

def _find_company_cfg(ticker: str) -> dict:
    """Znajduje konfigurację spółki (nazwa, symbol XTB) po tickerze - najpierw
    w watchliście, potem wśród ostatnio odkrytych przez AI kandydatów, a jako
    fallback buduje minimalną konfigurację z samego tickera."""
    ticker_upper = ticker.upper()
    for c in db.get_watchlist():
        if c["ticker"].upper() == ticker_upper:
            return c
    cached = db.load_results_cache()
    if cached:
        for r in (cached.get("discovered_results") or []):
            if r["ticker"].upper() == ticker_upper:
                return {"ticker": r["ticker"], "xtb_symbol": r.get("xtb_symbol", r["ticker"]),
                        "name": r.get("name", r["ticker"])}
    return {"ticker": ticker_upper, "xtb_symbol": f"{ticker_upper} (sprawdź w XTB)", "name": ticker_upper}


def _refresh_single_ticker_blocking(ticker: str) -> dict:
    """Pełna analiza JEDNEJ spółki na żądanie (ceny, RSI/ATR, sentyment LLM,
    fundamenty) - bez czekania na cały cykl watchlisty. Wynik podmienia
    odpowiedni wpis w cache'u i jest rozgłaszany przez WebSocket, żeby
    wszystkie otwarte karty przeglądarki od razu zobaczyły świeże dane."""
    company_cfg = _find_company_cfg(ticker)
    result = analyze_company(company_cfg, _cfg, threshold_adjustment=0.0,
                              on_progress=_sync_progress_to_broadcast)
    if result is None:
        raise ValueError(f"Nie udało się pobrać danych dla {ticker} (nieprawidłowy ticker lub błąd API).")
    result = sanitize_for_json(result)

    db.record_score_snapshot(result["ticker"], result["combined"]["final_score"],
                              result["combined"]["category"], result["technical"]["signal"],
                              source="manual_refresh",
                              market_price=result["technical"]["metrics"].get("last_price"))

    cached = db.load_results_cache() or {"results": [], "discovered_results": []}
    updated = False
    for key in ("results", "discovered_results"):
        items = cached.get(key) or []
        for i, r in enumerate(items):
            if r["ticker"].upper() == result["ticker"].upper():
                items[i] = result
                updated = True
                break
        cached[key] = items
    if not updated:
        cached.setdefault("results", []).append(result)

    cached["generated_at"] = datetime.now().isoformat(timespec="seconds")
    db.save_results_cache(cached["generated_at"], sanitize_for_json(cached))
    return result

def _get_enriched_open_positions(cached: dict | None = None) -> list[dict]:
    """Łączy otwarte pozycje portfela z najnowszym wynikiem analizy tej
    spółki - reużywane przez /api/portfolio, panel ryzyka i zapis krzywej
    kapitału, żeby nie duplikować tej samej logiki w trzech miejscach."""
    positions = db.get_portfolio(status="open")
    cached = cached if cached is not None else (db.load_results_cache() or {})
    by_ticker = {r["ticker"]: r for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])}
    enriched = []
    for p in positions:
        result = by_ticker.get(p["ticker"])
        evaluation = evaluate_portfolio_position(
            p, result,
            earnings_warning_days=_cfg.get("fundamentals", {}).get("earnings_warning_days", 7),
        )
        enriched.append({**p, **evaluation})
    return enriched

def _build_combined_portfolio_view(enriched_positions: list[dict], base_currency: str) -> tuple[list[dict], set]:
    """Przelicza kwoty pieniężne KAŻDEJ pozycji na base_currency, żeby móc
    je bezpiecznie zsumować (bez tego mieszalibyśmy np. USD i PLN jak tę
    samą jednostkę - patrz naprawiony bug w compute_portfolio_sector_exposure).
    Zwraca (przeliczone_pozycje, zbiór_walut_ktorych_nie_udalo_sie_przeliczyc).

    Koszt (cost_basis) używa REALNEGO kursu z transakcji (buy_fx_rate), jeśli
    jest znany (import XTB albo ręcznie wpisany przy dodawaniu pozycji) -
    broker dolicza własną marżę do kursu rynkowego, więc "ile naprawdę
    zapłaciłeś w PLN" różni się od przeliczenia dzisiejszym kursem. Bieżąca
    wartość (market_value) ZAWSZE liczona kursem AKTUALNYM - gdybyś sprzedał
    dziś, dostałbyś dzisiejszy kurs, nie ten z dnia zakupu."""
    converted = []
    skipped = set()
    for p in enriched_positions:
        currency = p.get("currency") or "USD"
        rate = get_fx_rate(currency, base_currency)
        if rate is None:
            skipped.add(currency)
            continue
        cost_rate = p.get("buy_fx_rate") or rate
        p2 = dict(p)
        for key in ("market_value", "current_price", "suggested_stop_loss"):
            if p2.get(key) is not None:
                p2[key] = p2[key] * rate
        if p2.get("cost_basis") is not None:
            p2["cost_basis"] = p2["cost_basis"] * cost_rate
        # Pochodne pola PRZELICZONE OD NOWA z już skonwertowanych market_value/
        # cost_basis, a nie przeskalowane jednym kursem jak dawniej - inaczej,
        # skoro koszt i wartość mogą teraz używać RÓŻNYCH kursów, unrealized_value
        # przestałby się zgadzać z market_value - cost_basis.
        if p2.get("market_value") is not None and p2.get("cost_basis") is not None:
            p2["unrealized_value"] = round(p2["market_value"] - p2["cost_basis"], 2)
            p2["unrealized_pct"] = (round(p2["unrealized_value"] / p2["cost_basis"] * 100, 2)
                                     if p2["cost_basis"] else None)
        p2["original_currency"] = currency  # potrzebne np. do wyboru benchmarku wg dominującej waluty
        p2["currency"] = base_currency  # dla compute_portfolio_risk_summary - jeden wspólny "koszyk"
        converted.append(p2)
    return converted, skipped

def _run_analysis_blocking() -> dict:
    watchlist = db.get_watchlist()
    cooldown_days = _cfg.get("discovery", {}).get("cooldown_days", 14)
    recently_proposed = db.get_recently_proposed_tickers(cooldown_days=cooldown_days)
    payload = run_full_analysis(_cfg, watchlist, on_progress=_sync_progress_to_broadcast,
                                 extra_excluded_tickers=recently_proposed)
    db.save_results_cache(payload["generated_at"], payload)

    for r in payload["discovered_results"]:
        db.record_discovery_proposal(r["ticker"])

    # Zapisujemy "zdjęcie" werdyktu każdej przeanalizowanej spółki - to
    # buduje historię, którą potem rysujemy jako znaczniki na wykresie ceny.
    open_position_tickers = {p["ticker"] for p in db.get_portfolio(status="open")}
    for r in payload["results"]:
        db.record_score_snapshot(r["ticker"], r["combined"]["final_score"],
                                  r["combined"]["category"], r["technical"]["signal"],
                                  source="watchlist",
                                  market_price=r["technical"]["metrics"].get("last_price"))
        if r["ticker"] in open_position_tickers:
            currency = r["technical"]["metrics"].get("currency")
            if currency:
                for p in db.get_portfolio(status="open"):
                    if p["ticker"] == r["ticker"]:
                        db.set_position_currency(p["id"], currency)
    for r in payload["discovered_results"]:
        db.record_score_snapshot(r["ticker"], r["combined"]["final_score"],
                                  r["combined"]["category"], r["technical"]["signal"],
                                  source="discovery",
                                  market_price=r["technical"]["metrics"].get("last_price"))

    # Krzywa kapitału portfela - "zdjęcie" łącznej wartości/kosztu per
    # waluta przy KAŻDYM pełnym cyklu (nie przy odświeżeniach pojedynczych
    # spółek), żeby punkty na krzywej były regularne w czasie.
    enriched_positions = _get_enriched_open_positions(payload)
    equity_by_currency: dict[str, dict] = {}
    for p in enriched_positions:
        currency = p.get("currency") or "USD"
        bucket = equity_by_currency.setdefault(currency, {"value": 0.0, "cost": 0.0})
        bucket["value"] += p.get("market_value") or 0.0
        bucket["cost"] += p.get("cost_basis") or 0.0
    for currency, totals in equity_by_currency.items():
        db.record_portfolio_equity_snapshot(currency, totals["value"], totals["cost"])

    # Widok ŁĄCZNY (wszystkie waluty przeliczone na jedną, base_currency) -
    # kursem AKTUALNYM W TYM CYKLU, więc historia w czasie jest dokładna.
    # ZAWSZE zapisujemy snapshot, nawet jeśli część walut nie dała się
    # przeliczyć - inaczej jedna chwilowo niedostępna para walutowa
    # (np. przejściowy problem Yahoo Finance) wstrzymywałaby całą krzywą
    # kapitału w nieskończoność, mimo że mamy częściowe, sensowne dane.
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    combined_positions, skipped_currencies = _build_combined_portfolio_view(enriched_positions, base_currency)
    combined_value = sum(p.get("market_value") or 0.0 for p in combined_positions)
    combined_cost = sum(p.get("cost_basis") or 0.0 for p in combined_positions)

    _sync_progress_to_broadcast(
        f"Widok łączny portfela: {len(combined_positions)}/{len(enriched_positions)} pozycji przeliczonych "
        f"na {base_currency}, wartość={combined_value:.2f}, koszt={combined_cost:.2f}.", "info"
    )
    db.record_portfolio_equity_combined_snapshot(base_currency, combined_value, combined_cost)

    if skipped_currencies:
        msg = (f"Nie udało się pobrać kursu dla walut {sorted(skipped_currencies)} -> {base_currency} - "
               f"pominięte w widoku łącznym portfela w tym cyklu (spróbuję ponownie w kolejnym).")
        logger.warning(msg)
        _sync_progress_to_broadcast(msg, "warning")

    # Codzienny brief AI - generowany na końcu, gdy mamy już PEŁEN obraz
    # (wyniki + portfel + ryzyko) do podsumowania w jednym spójnym tekście.
    portfolio_summary_for_brief = None
    if equity_by_currency:
        risk_data = compute_portfolio_risk_summary(enriched_positions)
        portfolio_summary_for_brief = {"by_currency": {}}
        for currency, totals in equity_by_currency.items():
            pl_pct = ((totals["value"] - totals["cost"]) / totals["cost"] * 100) if totals["cost"] > 0 else 0.0
            risk_for_currency = risk_data.get("by_currency", {}).get(currency, {})
            portfolio_summary_for_brief["by_currency"][currency] = {
                "value": totals["value"], "cost": totals["cost"], "pl_pct": pl_pct,
                "risk_pct": risk_for_currency.get("risk_pct"),
            }
        combined_pl_pct = ((combined_value - combined_cost) / combined_cost * 100) if combined_cost > 0 else 0.0
        portfolio_summary_for_brief["combined"] = {
            "base_currency": base_currency, "value": combined_value,
            "cost": combined_cost, "pl_pct": combined_pl_pct,
        }

    try:
        payload["daily_brief"] = generate_daily_brief(payload, portfolio_summary_for_brief, _cfg["llm"])
    except Exception:  # noqa: BLE001
        logger.exception("Błąd podczas generowania codziennego briefu - pomijam, reszta cyklu bez zmian.")
        payload["daily_brief"] = None

    db.save_results_cache(payload["generated_at"], payload)  # nadpisujemy z dołączonym briefem

    return payload


async def trigger_analysis_now() -> None:
    global _is_running, _next_run_at
    if _is_running:
        await manager.broadcast({"type": "progress", "level": "warning",
                                  "message": "Analiza już trwa - poczekaj na zakończenie bieżącego cyklu."})
        return
    _is_running = True
    await manager.broadcast({"type": "run_started", "message": "Rozpoczynam analizę..."})
    try:
        payload = await asyncio.to_thread(_run_analysis_blocking)
        await manager.broadcast({"type": "results", "payload": payload})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Błąd podczas analizy")
        await manager.broadcast({"type": "progress", "level": "error",
                                  "message": f"Błąd krytyczny podczas analizy: {exc}"})
    finally:
        _is_running = False
        interval_min = _cfg.get("web", {}).get("refresh_interval_minutes", 60)
        _next_run_at = datetime.now().timestamp() + interval_min * 60
        await manager.broadcast({"type": "run_finished", "next_run_at": _next_run_at})


async def _background_scheduler() -> None:
    """Pętla działająca przez cały czas życia serwera - uruchamia analizę
    natychmiast po starcie, a potem co `refresh_interval_minutes`."""
    global _next_run_at
    interval_min = _cfg.get("web", {}).get("refresh_interval_minutes", 60)
    run_on_startup = _cfg.get("web", {}).get("run_analysis_on_startup", True)

    if run_on_startup:
        await trigger_analysis_now()
    else:
        _next_run_at = datetime.now().timestamp() + interval_min * 60

    while True:
        await asyncio.sleep(30)  # sprawdzaj co 30s, czy pora na kolejny cykl
        if _next_run_at is not None and datetime.now().timestamp() >= _next_run_at and not _is_running:
            await trigger_analysis_now()


def _check_price_alerts_blocking() -> list[dict]:
    """Szybki, LEKKI sprawdzian - TYLKO cena (jedno zapytanie yfinance na
    spółkę w portfelu), bez LLM/fundamentów/Finnhuba. Osobny od pełnego
    cyklu analizy, żeby wykrywać przebicie stop-lossu/ceny docelowej dużo
    częściej niż raz na godzinę, bez obciążania kosztownych źródeł danych.
    Zwraca listę nowych alertów do wysłania (i aktualizuje _sent_price_alerts,
    żeby nie powtarzać tego samego alertu w kółko)."""
    positions = db.get_portfolio()
    if not positions:
        return []

    by_ticker: dict[str, list[dict]] = {}
    for p in positions:
        by_ticker.setdefault(p["ticker"], []).append(p)

    cached = db.load_results_cache() or {}
    fundamentals_by_ticker = {
        r["ticker"]: r.get("fundamentals")
        for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
    }

    new_alerts = []
    active_alert_keys = set()

    for ticker, lots in by_ticker.items():
        try:
            history = fetch_history(ticker, period="5d", interval="1d", use_cache=False)
            current_price = float(history["Close"].iloc[-1])
            currency = "USD"
            try:
                fast_info = yf.Ticker(ticker).fast_info
                currency = fast_info.get("currency") or "USD"
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alert cenowy: nie udało się pobrać ceny dla %s: %s", ticker, exc)
            continue

        fund = fundamentals_by_ticker.get(ticker)
        target_mean = None
        if fund and fund.get("available"):
            target_mean = fund.get("metrics", {}).get("analyst_target_mean")

        for p in lots:
            # Stop-loss przybliżony na podstawie ceny zakupu (ATR nie jest
            # tu liczony - to celowo LEKKI check; pełen ATR pochodzi z
            # ostatniego pełnego cyklu analizy, cache'owanego w wynikach).
            cached_result = next(
                (r for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
                 if r["ticker"] == ticker), None
            )
            stop_loss = (cached_result["technical"]["metrics"].get("suggested_stop_loss")
                         if cached_result else None)
            # Własny stop-loss użytkownika (jeśli ustawiony) ma pierwszeństwo nad ATR.
            custom_stop = p.get("custom_stop")
            if custom_stop:
                stop_loss = custom_stop
            stop_label = "własnego" if custom_stop else "sugerowanego"

            if stop_loss is not None and current_price < stop_loss:
                key = (p["id"], "stop_loss")
                active_alert_keys.add(key)
                if key not in _sent_price_alerts:
                    new_alerts.append({
                        "type": "stop_loss", "position_id": p["id"], "ticker": ticker,
                        "currency": currency, "current_price": round(current_price, 2),
                        "stop_loss": round(stop_loss, 2),
                        "title": f"{ticker}: cena poniżej stop-loss",
                        "body": f"Cena {current_price:.2f} {currency} spadła poniżej {stop_label} "
                                f"stop-loss ({stop_loss:.2f} {currency}). Pozycja: {p['shares']} szt. @ {p['buy_price']}.",
                    })

            custom_target = p.get("custom_target")
            if custom_target and current_price >= custom_target:
                key = (p["id"], "custom_target")
                active_alert_keys.add(key)
                if key not in _sent_price_alerts:
                    new_alerts.append({
                        "type": "custom_target", "position_id": p["id"], "ticker": ticker,
                        "currency": currency, "current_price": round(current_price, 2),
                        "target": round(custom_target, 2),
                        "title": f"{ticker}: osiągnięto Twój cel cenowy",
                        "body": f"Cena {current_price:.2f} {currency} osiągnęła ustawiony przez Ciebie cel "
                                f"({custom_target:.2f} {currency}). Pozycja: {p['shares']} szt. @ {p['buy_price']}.",
                    })

            if target_mean is not None and current_price >= target_mean:
                key = (p["id"], "target_reached")
                active_alert_keys.add(key)
                if key not in _sent_price_alerts:
                    new_alerts.append({
                        "type": "target_reached", "position_id": p["id"], "ticker": ticker,
                        "currency": currency, "current_price": round(current_price, 2),
                        "target": round(target_mean, 2),
                        "title": f"{ticker}: osiągnięto cenę docelową analityków",
                        "body": f"Cena {current_price:.2f} {currency} osiągnęła średnią cenę docelową "
                                f"analityków ({target_mean:.2f} {currency}).",
                    })

    # Czyścimy alerty dla sytuacji, które przestały być aktualne (np. cena
    # wróciła nad stop-loss) - żeby ewentualny PONOWNY spadek znów wysłał alert.
    stale = _sent_price_alerts - active_alert_keys
    _sent_price_alerts.difference_update(stale)
    _sent_price_alerts.update(active_alert_keys)

    # Powiadomienia zewnętrzne (Discord) - ta sama, już zdeduplikowana lista
    # co log/WebSocket, więc nie ma ryzyka zalania Discorda powtórkami tego
    # samego alertu. Cicho nic nie robi, jeśli notifications.enabled=false.
    notify_price_alerts(new_alerts, _cfg.get("notifications", {}))

    return new_alerts


async def _price_alert_scheduler() -> None:
    """Osobna, znacznie częstsza pętla niż pełny cykl analizy - sprawdza
    TYLKO ceny pozycji portfela, żeby wykryć przebicie stop-lossu/celu
    dużo szybciej niż raz na godzinę."""
    interval_sec = _cfg.get("web", {}).get("price_alert_interval_seconds", 180)
    if interval_sec <= 0:
        logger.info("Alerty cenowe wyłączone (price_alert_interval_seconds <= 0).")
        return

    while True:
        await asyncio.sleep(interval_sec)
        try:
            alerts = await asyncio.to_thread(_check_price_alerts_blocking)
            for alert in alerts:
                # UWAGA: alert ma własne pole "type" (stop_loss/target_reached/...).
                # Wcześniej {"type": "price_alert", **alert} nadpisywało typ wiadomości
                # WebSocket, więc frontend nigdy nie rozpoznawał alertu. Teraz typ
                # alertu jedzie w "alert_type", a "type" zostaje "price_alert".
                await manager.broadcast({**alert, "type": "price_alert", "alert_type": alert["type"]})
        except Exception:  # noqa: BLE001
            logger.exception("Błąd podczas sprawdzania alertów cenowych")


@app.post("/api/portfolio/check-alerts-now")
async def api_check_alerts_now():
    """Sprawdzenie alertów cenowych NA ŻĄDANIE - ta sama logika co
    _price_alert_scheduler (i te same dedupe/broadcast), tylko bez czekania
    na najbliższy tick pętli w tle (domyślnie do price_alert_interval_seconds,
    a PIERWSZE sprawdzenie po starcie serwera czeka pełen interwał - patrz
    komentarz w _price_alert_scheduler). Przydatne zaraz po zmianie
    stop-lossu/celu pozycji, i do ręcznego testu."""
    alerts = await asyncio.to_thread(_check_price_alerts_blocking)
    for alert in alerts:
        await manager.broadcast({**alert, "type": "price_alert", "alert_type": alert["type"]})
    return {"alerts_sent": len(alerts)}


@app.post("/api/notifications/test")
async def api_test_notification():
    """Wysyła JEDNĄ testową wiadomość na skonfigurowany kanał zewnętrzny
    (Discord), żeby zweryfikować webhook_url bez czekania na prawdziwy
    alert cenowy. Nie przechodzi przez _check_price_alerts_blocking - to
    celowo osobna, prostsza ścieżka."""
    notif_cfg = _cfg.get("notifications", {})
    webhook_url = notif_cfg.get("discord_webhook_url")
    if not notif_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="Powiadomienia są wyłączone (notifications.enabled: false w config.yaml).")
    if not webhook_url:
        raise HTTPException(status_code=400, detail="Brak discord_webhook_url w config.yaml.")

    ok = await asyncio.to_thread(
        send_discord_alert,
        "XTB Trend Watch: test powiadomień",
        "Jeśli to widzisz, webhook Discorda jest poprawnie skonfigurowany.",
        "test",
        webhook_url,
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Discord odrzucił wiadomość - sprawdź webhook_url i logi serwera.")
    return {"sent": True}


class AddCompanyRequest(BaseModel):
    ticker: str
    name: str = ""
    xtb_symbol: str = ""


class AddPositionRequest(BaseModel):
    ticker: str
    shares: float
    buy_price: float
    buy_date: str   # "YYYY-MM-DD"
    notes: str = ""
    custom_stop: float | None = None      # własny stop-loss (opcjonalnie)
    custom_target: float | None = None    # własna cena docelowa (opcjonalnie)
    # Rzeczywisty kurs wymiany (natywna waluta -> portfolio.base_currency)
    # zastosowany przy TEJ transakcji - opcjonalnie, gdy broker dolicza własną
    # marżę do kursu rynkowego (np. XTB), więc dzisiejszy/NBP kurs nie
    # odzwierciedla, ile faktycznie zapłacono. Patrz komentarz przy migracji
    # kolumn w db.init_db().
    buy_fx_rate: float | None = None


class ClosePositionRequest(BaseModel):
    sell_price: float
    sell_date: str  # "YYYY-MM-DD"


class ChatRequest(BaseModel):
    messages: list[dict]  # [{"role": "user"|"assistant", "content": "..."}]


@app.get("/api/watchlist")
async def api_get_watchlist():
    return db.get_watchlist()


@app.post("/api/watchlist")
async def api_add_company(req: AddCompanyRequest):
    ok = db.add_company(req.ticker, req.name or req.ticker.upper(),
                         xtb_symbol=req.xtb_symbol or None, source="manual")
    if not ok:
        raise HTTPException(status_code=409, detail=f"Spółka {req.ticker.upper()} już jest na watchliście.")
    await manager.broadcast({"type": "watchlist_changed"})
    return {"status": "ok", "ticker": req.ticker.upper()}


@app.delete("/api/watchlist/{ticker}")
async def api_remove_company(ticker: str):
    ok = db.remove_company(ticker)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Nie znaleziono spółki {ticker.upper()} na watchliście.")
    await manager.broadcast({"type": "watchlist_changed"})
    return {"status": "ok"}


@app.get("/api/results")
async def api_get_results():
    cached = db.load_results_cache()
    if not cached:
        return JSONResponse({"generated_at": None, "results": [], "discovered_results": [],
                              "macro_context": None, "macro_summary": ""})
    return cached


@app.get("/api/portfolio")
async def api_get_portfolio():
    return sanitize_for_json(_get_enriched_open_positions())

@app.post("/api/portfolio/import-xtb")
async def api_import_xtb(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Oczekiwano pliku .xlsx z eksportu XTB.")

    content = await file.read()
    try:
        parsed = await asyncio.to_thread(parse_xtb_report, content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Nie udało się odczytać pliku: {exc}")

    already_imported = db.get_imported_xtb_ids()
    imported_open = imported_closed = skipped_duplicates = fx_rates_backfilled = 0
    needs_review: list[str] = []

    for row in parsed["open"]:
        if row["xtb_id"] in already_imported:
            skipped_duplicates += 1
            # Pozycja już istnieje (np. zaimportowana ZANIM ta funkcja się
            # pojawiła) - uzupełnij tylko brakujący kurs, nic więcej. Bez
            # tego ponowny import tego samego pliku nigdy by nie donosił
            # rzeczywistego kursu do pozycji sprzed tej zmiany.
            if db.backfill_xtb_fx_rate(row["xtb_id"], row.get("buy_fx_rate"), None):
                fx_rates_backfilled += 1
            continue
        db.add_company(row["ticker"], name=row["ticker"], source="xtb_import")
        db.add_position_full(
            ticker=row["ticker"], shares=row["shares"], buy_price=row["buy_price"],
            buy_date=row["buy_date"], notes=f"Import XTB [XTB:{row['xtb_id']}]",
            status="open", currency=row["currency"], buy_fx_rate=row.get("buy_fx_rate"),
        )
        imported_open += 1
        if row["ticker"] == row["original_ticker"] and "." in row["original_ticker"]:
            pass  # mapowanie pewne, nic do zgłoszenia

    for row in parsed["closed"]:
        if row["xtb_id"] in already_imported:
            skipped_duplicates += 1
            if db.backfill_xtb_fx_rate(row["xtb_id"], row.get("buy_fx_rate"), row.get("sell_fx_rate")):
                fx_rates_backfilled += 1
            continue
        db.add_company(row["ticker"], name=row["ticker"], source="xtb_import")
        db.add_position_full(
            ticker=row["ticker"], shares=row["shares"], buy_price=row["buy_price"],
            buy_date=row["buy_date"], notes=f"Import XTB [XTB:{row['xtb_id']}]",
            status="closed", sell_price=row["sell_price"], sell_date=row["sell_date"],
            currency=row["currency"], buy_fx_rate=row.get("buy_fx_rate"),
            sell_fx_rate=row.get("sell_fx_rate"),
        )
        imported_closed += 1

    await manager.broadcast({"type": "watchlist_changed"})

    return {
        "imported_open": imported_open,
        "imported_closed": imported_closed,
        "skipped_duplicates": skipped_duplicates,
        "fx_rates_backfilled": fx_rates_backfilled,
        "warnings": parsed["warnings"],
    }

@app.get("/api/portfolio/risk")
async def api_get_portfolio_risk():
    enriched = _get_enriched_open_positions()
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    cached = db.load_results_cache() or {}
    fundamentals_by_ticker = {
        r["ticker"]: r.get("fundamentals")
        for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
    }

    risk = compute_portfolio_risk_summary(enriched)  # per-natywna-waluta, bez zmian

    # Widok łączny - konwersja na base_currency NAPRAWIA też stary bug:
    # ekspozycja sektorowa wcześniej sumowała market_value z różnych walut
    # BEZ przeliczenia (np. 100 USD + 100 PLN liczone jako "200" tej samej
    # jednostki) - teraz liczona na przeliczonych, spójnych kwotach.
    combined_positions, skipped = _build_combined_portfolio_view(enriched, base_currency)
    combined_value = sum(p.get("market_value") or 0.0 for p in combined_positions)
    combined_cost = sum(p.get("cost_basis") or 0.0 for p in combined_positions)
    combined_pl_pct = ((combined_value - combined_cost) / combined_cost * 100) if combined_cost > 0 else None
    combined_risk = compute_portfolio_risk_summary(combined_positions)
    combined_risk_bucket = combined_risk.get("by_currency", {}).get(base_currency, {})
    sector_exposure = compute_portfolio_sector_exposure(combined_positions, fundamentals_by_ticker)

    return sanitize_for_json({
        "risk": risk,
        "sector_exposure": sector_exposure,
        "combined": {
            "base_currency": base_currency,
            "value": round(combined_value, 2),
            "cost": round(combined_cost, 2),
            "pl_pct": round(combined_pl_pct, 2) if combined_pl_pct is not None else None,
            "risk_amount": combined_risk_bucket.get("risk_amount"),
            "risk_pct": combined_risk_bucket.get("risk_pct"),
            "skipped_currencies": sorted(skipped),
        },
    })


def _pick_portfolio_benchmark(combined_positions: list[dict]) -> str:
    """Benchmark do porównania z portfelem: jawny `portfolio.benchmark` z configu,
    a w razie jego braku benchmark właściwy dla DOMINUJĄCEJ waluty portfela
    (wg wartości rynkowej), z mapowania `technical.benchmark_by_currency`."""
    explicit = (_cfg.get("portfolio", {}) or {}).get("benchmark")
    if explicit:
        return str(explicit).strip()
    value_by_currency: dict[str, float] = {}
    for p in combined_positions:
        cur = p.get("original_currency") or "USD"
        value_by_currency[cur] = value_by_currency.get(cur, 0.0) + (p.get("market_value") or 0.0)
    dominant = max(value_by_currency, key=value_by_currency.get) if value_by_currency else "USD"
    return _cfg.get("technical", {}).get("benchmark_by_currency", {}).get(dominant, "SPY")


def _compute_portfolio_statistics_blocking(weights: dict[str, float], risk_free_rate_pct: float,
                                            benchmark_ticker: str | None = None) -> dict:
    closes = {}
    without_data = []
    for ticker in weights:
        try:
            closes[ticker] = fetch_history(ticker, period="1y", interval="1d")["Close"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Statystyki portfela: brak danych dla %s: %s", ticker, exc)
            without_data.append(ticker)
    stats = compute_portfolio_statistics(closes, {t: weights[t] for t in closes}, risk_free_rate_pct)
    stats["tickers_without_data"] = without_data

    # Porównanie z benchmarkiem - osobny blok; jego błąd nie może zepsuć reszty statystyk.
    # Ten sam łańcuch źródeł (główny ticker + zastępniki, np. Stooq dla ^WIG20) co przy
    # sile względnej w analysis_engine.py - jedna, spójna logika awaryjna w market_data.py.
    if benchmark_ticker:
        try:
            fallbacks = get_benchmark_fallbacks(benchmark_ticker, _cfg.get("technical", {}))
            bench_hist = fetch_benchmark_history(benchmark_ticker, period="1y", fallbacks=fallbacks)
            stats["benchmark_comparison"] = compute_benchmark_comparison(
                closes, {t: weights[t] for t in closes}, bench_hist.attrs.get("label", benchmark_ticker),
                bench_hist["Close"], risk_free_rate_pct,
            )
        except BenchmarkUnavailable as exc:
            logger.warning("Benchmark %s niedostępny (żadne źródło): %s", benchmark_ticker, exc)
            stats["benchmark_comparison"] = {
                "available": False,
                "reason": f"Benchmark {benchmark_ticker} chwilowo niedostępny (spróbuj ponownie za kilka minut).",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Porównanie z benchmarkiem %s nie powiodło się: %s", benchmark_ticker, exc)
            stats["benchmark_comparison"] = {
                "available": False, "reason": f"Nie udało się pobrać notowań benchmarku {benchmark_ticker}.",
            }
    return stats


@app.get("/api/portfolio/statistics")
async def api_get_portfolio_statistics():
    """Korelacje, zmienność, Sharpe i maks. obsunięcie portfela (1 rok historii,
    obecne wagi wg wartości rynkowej przeliczonej na walutę bazową)."""
    enriched = _get_enriched_open_positions()
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    combined_positions, _ = _build_combined_portfolio_view(enriched, base_currency)

    weights: dict[str, float] = {}
    for p in combined_positions:
        weights[p["ticker"]] = weights.get(p["ticker"], 0.0) + (p.get("market_value") or 0.0)
    weights = {t: v for t, v in weights.items() if v > 0}
    if not weights:
        return {"available": False, "reason": "Brak otwartych pozycji z policzoną wartością rynkową."}

    rf = float(_cfg.get("portfolio", {}).get("risk_free_rate_pct", 0.0))
    benchmark_ticker = _pick_portfolio_benchmark(combined_positions)
    stats = await asyncio.to_thread(_compute_portfolio_statistics_blocking, weights, rf, benchmark_ticker)
    return sanitize_for_json(stats)


@app.get("/api/backup/export")
async def api_export_backup():
    """Pobiera kopię zapasową (watchlista + portfel) jako plik JSON."""
    filename = f"xtb_trend_watch_backup_{datetime.now().strftime('%Y-%m-%d')}.json"
    return JSONResponse(
        content=db.export_backup(),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/backup/import")
async def api_import_backup(file: UploadFile = File(...)):
    """Wczytuje kopię zapasową - idempotentnie (duplikaty są pomijane)."""
    if not file.filename.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Oczekiwano pliku .json z kopią zapasową.")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Plik jest za duży (limit 5 MB).")
    try:
        data = json.loads(content.decode("utf-8"))
        result = db.import_backup(data)
    except (ValueError, UnicodeDecodeError) as exc:  # JSONDecodeError dziedziczy po ValueError
        raise HTTPException(status_code=400, detail=f"Nie udało się wczytać kopii: {exc}")
    await manager.broadcast({"type": "watchlist_changed"})
    return result


@app.get("/api/portfolio/sparkline/{ticker}")
async def api_get_sparkline(ticker: str):
    try:
        history = await asyncio.to_thread(fetch_history, ticker, "1mo", "1d")
        closes = [round(float(c), 4) for c in history["Close"].dropna().tolist()]
    except Exception:  # noqa: BLE001
        closes = []
    return {"closes": closes}


@app.get("/api/portfolio/equity-currencies")
async def api_get_equity_currencies():
    return db.get_equity_currencies()


@app.get("/api/portfolio/equity/combined")
async def api_get_portfolio_equity_combined():
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    points = db.get_portfolio_equity_combined_curve(base_currency)
    drawdown = compute_max_drawdown(points)
    # TWR (time-weighted return) - w odróżnieniu od surowej wartości/kosztu
    # wyżej, dokupienie/sprzedaż pozycji nie zniekształca wyniku, więc to
    # jest uczciwa liczba do porównania z benchmarkiem (patrz report.py).
    # Przepływy ze WSZYSTKICH walut przeliczone na base_currency kursem
    # historycznym z dnia zdarzenia - stąd asyncio.to_thread (zapytania FX).
    raw_flows = db.get_position_cash_flows()
    twr_points = await asyncio.to_thread(
        lambda: compute_twr_curve(points, prepare_cash_flows_for_currency(raw_flows, base_currency, True))
    )
    return sanitize_for_json({
        "points": points, "drawdown": drawdown, "base_currency": base_currency, "twr_points": twr_points,
    })


@app.get("/api/portfolio/equity/{currency}")
async def api_get_portfolio_equity(currency: str):
    points = db.get_portfolio_equity_curve(currency)
    drawdown = compute_max_drawdown(points)
    # TWR per-walutowy - przepływy są już w tej samej walucie co krzywa,
    # więc bez konwersji FX (convert=False) i bez potrzeby osobnego wątku.
    raw_flows = db.get_position_cash_flows(currency=currency.upper())
    cash_flows = prepare_cash_flows_for_currency(raw_flows, currency.upper(), convert=False)
    twr_points = compute_twr_curve(points, cash_flows)
    return sanitize_for_json({"points": points, "drawdown": drawdown, "twr_points": twr_points})


@app.get("/api/portfolio/closed")
async def api_get_closed_portfolio():
    positions = db.get_portfolio(status="closed")
    for p in positions:
        p["realized_pl"] = round((p["sell_price"] - p["buy_price"]) * p["shares"], 2)
        p["realized_pl_pct"] = round((p["sell_price"] - p["buy_price"]) / p["buy_price"] * 100, 2)
        try:
            buy_d = datetime.strptime(p["buy_date"], "%Y-%m-%d")
            sell_d = datetime.strptime(p["sell_date"], "%Y-%m-%d")
            p["holding_days"] = (sell_d - buy_d).days
        except (ValueError, TypeError):
            p["holding_days"] = None
    return sanitize_for_json({"positions": positions, "summary": db.get_closed_summary()})

@app.get("/api/portfolio/tax-summary")
async def api_get_tax_summary():
    closed = db.get_portfolio(status="closed")
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    # Wywołania kursów historycznych (yfinance) mogą chwilę potrwać przy
    # wielu transakcjach - w osobnym wątku, żeby nie blokować pętli asyncio.
    summary = await asyncio.to_thread(compute_tax_summary, closed, base_currency)
    return sanitize_for_json(summary)


@app.get("/api/portfolio/closed/export")
async def api_export_closed_trades_csv():
    """CSV historii zamkniętych transakcji - do wklejenia we własny arkusz
    rozliczenia podatkowego. ';' jako separator (domyślny separator listy
    w polskich ustawieniach regionalnych Excela), BOM na początku pliku,
    żeby polskie znaki (nazwy tickerów/notatki) wyświetliły się poprawnie
    po otwarciu w Excelu zamiast krzaczków."""
    closed = db.get_portfolio(status="closed")
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    header, rows = await asyncio.to_thread(build_closed_trades_export_rows, closed, base_currency)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)

    filename = f"xtb_trend_watch_zamkniete_transakcje_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/api/portfolio/{position_id}/close")
async def api_close_position(position_id: int, req: ClosePositionRequest):
    if req.sell_price <= 0:
        raise HTTPException(status_code=400, detail="Nieprawidłowa cena sprzedaży.")
    ok = db.close_position(position_id, req.sell_price, req.sell_date)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.post("/api/portfolio/{position_id}/reopen")
async def api_reopen_position(position_id: int):
    ok = db.reopen_position(position_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.post("/api/portfolio")
async def api_add_position(req: AddPositionRequest):
    ticker = req.ticker.strip().upper()
    if not ticker or req.shares <= 0 or req.buy_price <= 0:
        raise HTTPException(status_code=400, detail="Nieprawidłowe dane pozycji.")
    for level in (req.custom_stop, req.custom_target, req.buy_fx_rate):
        if level is not None and level <= 0:
            raise HTTPException(status_code=400, detail="Własny stop-loss, cel i kurs wymiany muszą być dodatnie.")
    # Jeśli spółki nie ma jeszcze na watchliście, dodajemy ją automatycznie,
    # żeby zaczęła być analizowana w kolejnych cyklach (inaczej nigdy nie
    # dostaniemy aktualnej ceny/sygnału do oceny tej pozycji).
    db.add_company(ticker, name=ticker, source="portfolio")
    position_id = db.add_position(ticker, req.shares, req.buy_price, req.buy_date, req.notes,
                                   custom_stop=req.custom_stop, custom_target=req.custom_target,
                                   buy_fx_rate=req.buy_fx_rate)

    # Ustalamy walutę od razu, jeśli mamy ją w cache'u z ostatniej analizy -
    # inaczej pozycja domyślnie pokazuje USD do najbliższego cyklu.
    cached = db.load_results_cache() or {}
    match = next((r for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
                  if r["ticker"] == ticker), None)
    if match:
        currency = match["technical"]["metrics"].get("currency")
        if currency:
            db.set_position_currency(position_id, currency)

    return {"status": "ok", "id": position_id}


@app.delete("/api/portfolio/{position_id}")
async def api_remove_position(position_id: int):
    ok = db.remove_position(position_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.put("/api/portfolio/{position_id}")
async def api_update_position(position_id: int, req: AddPositionRequest):
    ticker = req.ticker.strip().upper()
    if not ticker or req.shares <= 0 or req.buy_price <= 0:
        raise HTTPException(status_code=400, detail="Nieprawidłowe dane pozycji.")
    for level in (req.custom_stop, req.custom_target, req.buy_fx_rate):
        if level is not None and level <= 0:
            raise HTTPException(status_code=400, detail="Własny stop-loss, cel i kurs wymiany muszą być dodatnie.")
    db.add_company(ticker, name=ticker, source="portfolio")
    ok = db.update_position(position_id, ticker, req.shares, req.buy_price, req.buy_date, req.notes,
                             custom_stop=req.custom_stop, custom_target=req.custom_target,
                             buy_fx_rate=req.buy_fx_rate)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.get("/api/logs")
async def api_get_logs(limit: int = 100):
    return db.get_recent_logs(limit=limit)

@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    cached = db.load_results_cache() or {}
    enriched_positions = _get_enriched_open_positions(cached)
    portfolio_summary = summarize_portfolio_by_currency(enriched_positions)

    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    combined_positions, _ = _build_combined_portfolio_view(enriched_positions, base_currency)
    fundamentals_by_ticker = {
        r["ticker"]: r.get("fundamentals")
        for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
    }

    portfolio_risk = compute_portfolio_risk_summary(enriched_positions)
    sector_exposure = compute_portfolio_sector_exposure(combined_positions, fundamentals_by_ticker)

    closed_positions = db.get_portfolio(status="closed")
    tax_summary = (await asyncio.to_thread(compute_tax_summary, closed_positions, base_currency)
                   if closed_positions else None)

    effectiveness_stats = db.get_effectiveness_stats(
        min_age_days=_cfg.get("effectiveness", {}).get("min_signal_age_days", 14)
    )

    extra_context = {
        "portfolio_risk": portfolio_risk,
        "sector_exposure": sector_exposure,
        "tax_summary": tax_summary,
        "effectiveness_stats": effectiveness_stats,
    }

    reply = await asyncio.to_thread(
        answer_chat_question, req.messages, cached, portfolio_summary, extra_context, _cfg["llm"]
    )
    return {"reply": reply}

@app.get("/api/score-history/{ticker}")
async def api_get_score_history(ticker: str, limit: int = 400):
    """Historia werdyktów (kategoria/wynik/sygnał) dla spółki w czasie -
    używana do rysowania znaczników na wykresie ceny."""
    return db.get_score_history(ticker, limit=limit)

@app.get("/api/effectiveness")
async def api_get_effectiveness():
    """Statystyki skuteczności narzędzia - porównanie przeszłych werdyktów
    z późniejszą ceną tej samej spółki (patrz db.get_effectiveness_stats)."""
    min_age_days = _cfg.get("effectiveness", {}).get("min_signal_age_days", 14)
    return db.get_effectiveness_stats(min_age_days=min_age_days)


@app.get("/api/status")
async def api_get_status():
    return {"is_running": _is_running, "next_run_at": _next_run_at,
             "refresh_interval_minutes": _cfg.get("web", {}).get("refresh_interval_minutes", 60)}


@app.post("/api/run-now")
async def api_run_now():
    asyncio.create_task(trigger_analysis_now())
    return {"status": "started"}

@app.post("/api/refresh/{ticker}")
async def api_refresh_ticker(ticker: str):
    """Odświeża dane JEDNEJ spółki na żądanie (np. przy otwarciu jej karty
    na dashboardzie), zamiast czekać na cały cykl analizy watchlisty.
    Chronione cooldownem, żeby nie bombardować yfinance/Finnhub przy
    wielokrotnym otwieraniu tej samej spółki w krótkim czasie."""
    ticker_upper = ticker.upper()
    now = datetime.now().timestamp()
    cooldown = _cfg.get("web", {}).get("manual_refresh_cooldown_seconds", 45)
    last = _last_manual_refresh.get(ticker_upper)
    if last is not None and (now - last) < cooldown:
        wait = round(cooldown - (now - last))
        raise HTTPException(status_code=429,
                             detail=f"Poczekaj jeszcze {wait}s przed kolejnym odświeżeniem {ticker_upper}.")
    _last_manual_refresh[ticker_upper] = now

    try:
        result = await asyncio.to_thread(_refresh_single_ticker_blocking, ticker_upper)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Błąd odświeżania {ticker_upper}: {exc}")

    await manager.broadcast({"type": "ticker_updated", "result": result})
    return result


@app.get("/api/chart/{ticker}")
async def api_get_chart(ticker: str, period: str = "1y"):
    """Dane OHLC do wykresu świecowego (TradingView Lightweight Charts)."""
    try:
        history = await asyncio.to_thread(fetch_history, ticker, period, "1d")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"Nie udało się pobrać danych dla {ticker}: {exc}")

    candles = [
        {
            "time": idx.strftime("%Y-%m-%d"),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
        }
        for idx, row in history.iterrows()
    ]
    ma50 = history["Close"].rolling(50).mean()
    ma200 = history["Close"].rolling(200).mean()
    ma50_series = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
        for idx, v in ma50.items() if v == v  # odfiltruj NaN
    ]
    ma200_series = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
        for idx, v in ma200.items() if v == v
    ]

    return sanitize_for_json(
    {"ticker": ticker.upper(), "candles": candles, "ma50": ma50_series, "ma200": ma200_series}
)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # nie oczekujemy nic konkretnego od klienta - trzymamy połączenie żywe
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# Statyczny frontend (musi być zamontowany PO endpointach /api i /ws)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main():
    global _cfg
    parser = argparse.ArgumentParser(description="XTB Trend Watch - dashboard webowy")
    parser.add_argument("--config", default="config.yaml", help="Ścieżka do pliku konfiguracyjnego")
    parser.add_argument("--host", default=None, help="Nadpisuje web.host z configu")
    parser.add_argument("--port", type=int, default=None, help="Nadpisuje web.port z configu")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        _cfg = yaml.safe_load(f)

    web_cfg = _cfg.get("web", {})
    host = args.host or web_cfg.get("host", "127.0.0.1")
    port = args.port or web_cfg.get("port", 8000)

    import uvicorn
    print(f"\n>>> Dashboard dostępny pod adresem: http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
