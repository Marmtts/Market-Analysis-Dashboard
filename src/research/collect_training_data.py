"""
collect_training_data.py
---------------------------
FAZA 1 projektu "Ollama się uczy": budowa realnego, etykietowanego zbioru
danych (nagłówek -> faktyczny późniejszy ruch ceny), który jest niezbędnym
fundamentem PRZED jakimkolwiek fine-tuningiem.

Dla każdego historycznego nagłówka (Finnhub) o danej spółce:
1. Znajduje cenę w dniu publikacji i N sesji później (5/10/20).
2. Liczy rzeczywisty zwrot procentowy w tym oknie.
3. Puszcza OBECNY lokalny LLM na tym nagłówku, żeby ocenić sentyment
   "z perspektywy tamtego dnia" - to pozwala PÓŹNIEJ sprawdzić (skryptem
   calibration_report.py), czy sentyment LLM w ogóle koreluje z tym, co
   faktycznie stało się z ceną.

WAŻNE: to może być wolne - jedno wywołanie LLM na KAŻDY nagłówek. Dla
kilkuset nagłówków może to potrwać dziesiątki minut do kilku godzin,
zależnie od szybkości Twojego modelu. Można to zostawić działające w tle.

Użycie:
    python -m src.collect_training_data --config config.yaml
    python -m src.collect_training_data --tickers MSFT,AAPL --years 2
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import yaml
from rich.console import Console

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src import db
from src.market_data import fetch_history
from src.llm_sentiment import _call_ollama, _lexicon_sentiment
from src.news_sources import Headline

db.init_db()  # upewnia się, że wszystkie tabele (w tym training_examples) istnieją

import requests

console = Console()
FINNHUB_BASE_URL = "https://finnhub.io/api/v1/company-news"


def _fetch_daily_headlines(ticker: str, api_key: str, days_back: int,
                            request_delay_sec: float = 1.1) -> list[Headline]:
    """Jak news_history.fetch_historical_news_by_month, ale zwraca płaską
    listę nagłówków z ZACHOWANĄ dokładną datą dzienną (potrzebne tu do
    precyzyjnego dopasowania ceny z konkretnego dnia, nie całego miesiąca)."""
    today = date.today()
    start = today - timedelta(days=days_back)
    headlines: list[Headline] = []

    cursor = start
    while cursor < today:
        chunk_end = min(cursor + timedelta(days=30), today)
        params = {"symbol": ticker, "from": cursor.isoformat(), "to": chunk_end.isoformat(), "token": api_key}
        try:
            resp = requests.get(FINNHUB_BASE_URL, params=params, timeout=20)
            if resp.status_code == 403:
                console.print(f"[yellow]Finnhub 403 dla {ticker} - pomijam resztę okresu.[/yellow]")
                return headlines
            resp.raise_for_status()
            items = resp.json() or []
            for item in items:
                title = item.get("headline", "")
                if not title:
                    continue
                ts = item.get("datetime")
                pub_date = date.fromtimestamp(ts).isoformat() if ts else cursor.isoformat()
                headlines.append(Headline(
                    title=db._sanitize_text(title),
                    source=db._sanitize_text(item.get("source", "Finnhub")),
                    link=item.get("url", ""), published=pub_date,
                ))
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Błąd Finnhub dla {ticker} ({cursor}-{chunk_end}): {exc}[/yellow]")
        time.sleep(request_delay_sec)
        cursor = chunk_end + timedelta(days=1)

    return headlines


def _price_on_or_after(history, target_date_str: str):
    """Zwraca (data, cena zamknięcia) pierwszej sesji giełdowej w dniu
    target_date_str lub najbliższej PÓŹNIEJSZEJ - unika patrzenia wstecz."""
    for idx in history.index:
        if idx.strftime("%Y-%m-%d") >= target_date_str:
            return idx, float(history.loc[idx, "Close"])
    return None, None


def _forward_price(history, entry_idx_pos: int, sessions_ahead: int):
    target_pos = entry_idx_pos + sessions_ahead
    if target_pos >= len(history):
        return None
    return float(history["Close"].iloc[target_pos])


def collect_for_ticker(ticker: str, api_key: str, years: int, llm_cfg: dict) -> int:
    days_back = years * 365
    console.print(f"Pobieram historyczne nagłówki dla {ticker}...")
    headlines = _fetch_daily_headlines(ticker, api_key, days_back)
    if not headlines:
        console.print(f"[yellow]Brak nagłówków historycznych dla {ticker} - pomijam.[/yellow]")
        return 0

    console.print(f"Pobieram historię cen dla {ticker}...")
    try:
        history = fetch_history(ticker, period=f"{years + 1}y", interval="1d")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Nie udało się pobrać cen dla {ticker}: {exc}[/yellow]")
        return 0

    date_to_pos = {idx.strftime("%Y-%m-%d"): pos for pos, idx in enumerate(history.index)}
    sorted_dates = sorted(date_to_pos.keys())

    inserted = 0
    console.print(f"Analizuję sentyment LLM dla {len(headlines)} nagłówków (może potrwać długo)...")

    skipped_existing = 0
    for i, h in enumerate(headlines):
        entry_date, entry_price = _price_on_or_after(history, h.published)
        if entry_date is None:
            continue
        entry_pos = date_to_pos.get(entry_date.strftime("%Y-%m-%d"))
        if entry_pos is None:
            continue

        # Wznowienie po przerwanym uruchomieniu - pomijamy nagłówki już
        # zapisane w bazie, żeby nie płacić ponownie kosztem wywołania LLM.
        if db.training_example_exists(ticker, h.title, entry_date.strftime("%Y-%m-%d")):
            skipped_existing += 1
            continue

        fwd_5 = _forward_price(history, entry_pos, 5)
        fwd_10 = _forward_price(history, entry_pos, 10)
        fwd_20 = _forward_price(history, entry_pos, 20)

        def pct(fwd):
            return round((fwd - entry_price) / entry_price * 100, 2) if fwd is not None else None

        # Sentyment LLM "z perspektywy tamtego dnia" - jeden nagłówek naraz,
        # żeby ocena była jak najbardziej precyzyjnie przypisana do TEGO newsa.
        prompt = (
            f"Oceń sentyment tego pojedynczego nagłówka finansowego dla inwestora "
            f"rozważającego zakup akcji {ticker}. Zwróć WYŁĄCZNIE JSON: "
            f'{{"score": <-1.0 do 1.0>, "summary": "<1 zdanie po polsku>"}}\n\n'
            f"Nagłówek: {h.title}"
        )
        parsed = _call_ollama(prompt=prompt, base_url=llm_cfg["base_url"],
                               model=llm_cfg["model"], timeout=llm_cfg.get("request_timeout_seconds", 60))
        if parsed and "score" in parsed:
            try:
                llm_score = max(-1.0, min(1.0, float(parsed["score"])))
                llm_summary = str(parsed.get("summary", ""))[:300]
            except (TypeError, ValueError):
                llm_score, llm_summary = None, ""
        else:
            # fallback słownikowy - żeby przykład nie przepadł, jeśli LLM zawiedzie
            lex = _lexicon_sentiment([h])
            llm_score, llm_summary = lex.score, lex.summary

        db.add_training_example({
            "ticker": ticker, "headline": h.title, "source": h.source,
            "published_date": entry_date.strftime("%Y-%m-%d"),
            "price_at_headline": round(entry_price, 2),
            "price_fwd_5d": round(fwd_5, 2) if fwd_5 else None,
            "price_fwd_10d": round(fwd_10, 2) if fwd_10 else None,
            "price_fwd_20d": round(fwd_20, 2) if fwd_20 else None,
            "return_pct_5d": pct(fwd_5), "return_pct_10d": pct(fwd_10), "return_pct_20d": pct(fwd_20),
            "llm_score": llm_score, "llm_summary": llm_summary,
        })
        inserted += 1

        if (i + 1) % 20 == 0:
            console.print(f"  [dim]...{i + 1}/{len(headlines)} nagłówków przetworzonych[/dim]")

    if skipped_existing:
        console.print(f"[dim]Pominięto {skipped_existing} nagłówków już wcześniej zapisanych (wznowienie).[/dim]")
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Faza 1: zbieranie danych treningowych (nagłówek -> faktyczny zwrot)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tickers", default=None, help="Lista tickerów oddzielonych przecinkami")
    parser.add_argument("--years", type=int, default=2)
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    api_key = cfg["news"].get("finnhub_api_key", "")
    if not api_key or api_key in ("TWOJ_KLUCZ", "TWOJ_KLUCZ_FINNHUB_TUTAJ"):
        console.print("[red]Brak prawidłowego klucza Finnhub w config.yaml - bez niego nie da się pobrać "
                       "historycznych nagłówków. Zarejestruj się na finnhub.io i uzupełnij news.finnhub_api_key.[/red]")
        return

    tickers = (args.tickers.split(",") if args.tickers
               else [c["ticker"] for c in cfg.get("watchlist", [])])
    tickers = [t.strip() for t in tickers if t.strip()]

    console.rule(f"[bold cyan]Zbieranie danych treningowych - {len(tickers)} spółek, {args.years} lat[/bold cyan]")
    total = 0
    for ticker in tickers:
        count = collect_for_ticker(ticker, api_key, args.years, cfg["llm"])
        console.print(f"[green]{ticker}: zebrano {count} przykładów.[/green]")
        total += count

    console.rule("[bold green]Zakończono[/bold green]")
    console.print(f"Łącznie w bazie: {db.count_training_examples()} przykładów treningowych.")
    console.print("\nNastępny krok: uruchom [bold]python -m src.calibration_report[/bold], "
                   "żeby sprawdzić, czy obecny model LLM w ogóle trafnie ocenia sentyment "
                   "(Faza 2) - zanim zdecydujesz o fine-tuningu.")


if __name__ == "__main__":
    main()