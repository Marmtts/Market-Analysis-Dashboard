# XTB Trend Watch

Osobiste narzędzie analityczne do **codziennej analizy** wybranych spółek
(globalnie) pod kątem technicznym, sentymentu newsów, fundamentów i szerokiego
kontekstu makro — z pełnym dashboardem webowym do zarządzania watchlistą
i portfelem, oraz interaktywnym asystentem AI działającym w 100% lokalnie.

> ⚠️ **To nie jest system automatycznego handlu ani porada inwestycyjna.**
> Narzędzie generuje analizę, sygnały i raporty, które czytasz i na ich
> podstawie **ręcznie** podejmujesz decyzję oraz składasz zlecenie u swojego
> brokera. Nie łączy się z żadnym kontem maklerskim i nie handluje za Ciebie.

---

## 1. Co potrafi narzędzie

**Analiza spółek:**
- Wskaźniki techniczne na dwóch interwałach (dzienny + tygodniowy jako
  potwierdzenie): RSI, SMA, wstęgi Bollingera, ATR, dystans od 52-tyg.
  maksimum, wykrywanie gwałtownych spadków ("spadający nóż") i siła
  względna wobec benchmarku (S&P500 / WIG20 / inne, w zależności od waluty).
- Sentyment newsów (bieżący + trend 12-miesięczny) przez **lokalny LLM**
  (Ollama) z fallbackiem słownikowym offline.
- Podstawowa analiza fundamentalna: P/E, wzrost przychodów, marże,
  zadłużenie, cena docelowa i rekomendacja analityków Wall Street.
- Szeroki kontekst makro (VIX, rentowność obligacji) jako filtr ryzyka.
- Automatyczna blokada sugestii kupna blisko szczytu trendu lub tuż po
  gwałtownym spadku ceny — z osobną, łagodniejszą logiką dla ETF-ów.
- LLM może samodzielnie proponować nowe spółki do obserwacji na bazie
  newsów makro (moduł "discovery"), z cooldownem przeciw powtarzaniu
  tych samych propozycji.
- Osobny skrypt do backtestu strategii wejścia na danych historycznych,
  z trybem grid search i walidacją walk-forward (trening/test).

**Portfel i zarządzanie ryzykiem:**
- Śledzenie otwartych pozycji w wielu walutach jednocześnie, z automatycznym
  wykrywaniem waluty notowania każdej spółki.
- Podsumowanie łączne (wszystkie waluty przeliczone na jedną walutę bazową)
  obok podsumowania per waluta.
- Panel ryzyka: ile realnie stracisz przy obecnych stop-lossach, ekspozycja
  sektorowa Twojego realnego kapitału.
- Krzywa kapitału (equity curve) z maksymalnym historycznym obsunięciem
  (drawdown), per waluta i łączna.
- Historia zamkniętych transakcji + orientacyjne podsumowanie podatkowe
  (podatek od zysków kapitałowych, PIT-38) liczone **oficjalnym kursem NBP**
  zgodnie z art. 11a ustawy o PIT.
- Kalkulator wielkości pozycji (na bazie ATR) przy każdej analizowanej spółce.
- Alerty cenowe niezależne od pełnego cyklu (sprawdzanie samej ceny co
  kilka minut, bez angażowania LLM/newsów).

**Dashboard i asystent:**
- Pełny dashboard webowy (FastAPI + WebSocket) z watchlistą, portfelem,
  panelem skuteczności narzędzia i logiem na żywo.
- Codzienny brief AI — krótkie podsumowanie sytuacji generowane przez
  lokalny LLM na koniec każdego cyklu.
- Interaktywny czat z lokalnym LLM, który odpowiada na pytania na
  podstawie WSZYSTKICH danych z bieżącego cyklu (wyniki, portfel, ryzyko,
  podatki, skuteczność) — bez wysyłania czegokolwiek na zewnątrz.
- Wielowarstwowy cache (newsy historyczne, kursy walut, ceny) znacząco
  skracający czas kolejnych cykli analizy.

---

## 2. Instalacja

```bash
git clone <adres-twojego-repo>
cd xtb_trend_watch
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Skopiuj przykładową konfigurację i uzupełnij własnymi danymi:

```bash
cp config.example.yaml config.yaml
```

W `config.yaml` uzupełnij co najmniej:
- `watchlist` — swoją listę spółek (ticker w formacie Yahoo Finance),
- `news.finnhub_api_key` — jeśli chcesz newsy historyczne (sekcja 5),
  w przeciwnym razie ustaw `news.use_historical_news: false`.

> `config.yaml` zawiera Twoje dane i klucze API — jest w `.gitignore`
> i nigdy nie powinien trafić do repozytorium.

---

## 3. Uruchomienie

### Dashboard webowy (zalecany sposób korzystania)

```bash
python -m src.web_app --config config.yaml
```

Konsola pokaże adres (domyślnie `http://127.0.0.1:8000`) — otwórz go
w przeglądarce. Serwer musi działać cały czas, żeby automatyczne cykle
analizy, alerty cenowe i dashboard na żywo działały w tle.

Częstotliwość automatycznego cyklu i inne ustawienia webowe konfigurujesz
w sekcji `web:` pliku `config.yaml`.

### CLI (jednorazowa analiza + raport)

```bash
python -m src.main --config config.yaml
```

Raport pojawi się w terminalu oraz jako pliki `.md`/`.json` w katalogu
`reports/`.

### Uruchamianie codziennie automatycznie (CLI, bez dashboardu)

**Linux/macOS (cron):**
```bash
crontab -e
0 8 * * 1-5 cd /pełna/ścieżka/xtb_trend_watch && venv/bin/python -m src.main >> logs/cron.log 2>&1
```

**Windows:** użyj dołączonego `run_daily.bat` przez Harmonogram zadań
(szczegóły w komentarzach w samym pliku).

---

## 4. Konfiguracja lokalnego LLM (Ollama)

Do sentymentu newsów, codziennego briefu, propozycji AI (discovery) i czatu
z asystentem potrzebny jest lokalny model przez [Ollama](https://ollama.com):

```bash
# Linux
curl -fsSL https://ollama.com/install.sh | sh
# macOS/Windows: pobierz instalator z ollama.com/download

ollama pull llama3.1:8b
ollama serve
```

Domyślny adres `http://localhost:11434` zgadza się z `config.yaml`
(`llm.base_url`) — nic więcej nie trzeba zmieniać. Jeśli wolisz działać
w 100% offline bez LLM, ustaw `llm.enabled: false` — sentyment przełączy się
na prosty analizator słownikowy, a brief dnia, discovery i czat po prostu
się nie pojawią.

**Czat z asystentem** dostaje na raz dużo danych (cała watchlista + portfel),
dlatego w configu jest podniesiony limit kontekstu (`llm.chat_num_ctx`,
domyślnie 8192) — jeśli odpowiedzi zaczynają być chaotyczne przy większej
watchliście, rozważ większy model (np. `llama3.1:70b`, jeśli masz sprzęt)
lub dalsze podniesienie tego limitu kosztem szybkości odpowiedzi.

---

## 5. Newsy historyczne (Finnhub) i cache

Domyślne źródła (`yfinance` + RSS) dają tylko migawkę najświeższych
nagłówków. Żeby narzędzie widziało też trend sentymentu z ostatniego roku,
potrzebny jest darmowy klucz [Finnhub](https://finnhub.io/register),
wklejony w `config.yaml` (`news.finnhub_api_key`).

**Ważne o wydajności:** newsy historyczne są cache'owane trwale w lokalnej
bazie SQLite — miesiące starsze niż bieżący pobierane są z Finnhub **tylko
raz**, przy każdym kolejnym cyklu czytane lokalnie. Podobnie kursy walut
(bieżące i historyczne) oraz ceny (krótkoterminowo) — dzięki temu drugi
i kolejne cykle analizy są znacząco szybsze niż pierwszy.

Jeśli nie chcesz newsów historycznych, ustaw `news.use_historical_news: false`
— narzędzie będzie działać w pełni offline (poza pobieraniem cen), korzystając
z prostego analizatora słownikowego.

---

## 6. Mapowanie spółek na symbole brokera

W `config.yaml` każda spółka ma dwa pola:
```yaml
- ticker: "AAPL"          # symbol do pobierania danych (Yahoo Finance)
  xtb_symbol: "AAPL.US"   # notatka pomocnicza - jak szukać instrumentu u brokera
  name: "Apple Inc."
```
Typowe sufiksy giełd dla tickerów spoza USA: `.WA` (Warszawa), `.DE`
(Frankfurt), `.AS` (Amsterdam), `.L` (Londyn), `.T` (Tokio), `.PA` (Paryż).

---

## 7. Ograniczenia (przeczytaj, zanim zaufasz wynikom)

- To narzędzie analityczne, nie doradztwo inwestycyjne ani podatkowe —
  wszystkie decyzje i związane z nimi ryzyko leżą po stronie użytkownika.
- Analiza fundamentalna to szybki, regułowy zestaw wskaźników — nie
  zastępuje pełnej wyceny spółki (DCF, analizy sprawozdań finansowych).
- Backtest testuje wyłącznie logikę techniczną, bez newsów/sentymentu/
  fundamentów, i nie modeluje prowizji/spreadu/poślizgu cenowego.
- Podsumowanie podatkowe jest orientacyjne — nawet przy użyciu oficjalnego
  kursu NBP nie zastępuje samodzielnego rozliczenia PIT-38 ani konsultacji
  z doradcą podatkowym.
- Sentyment newsów zależy od jakości dostępnych źródeł (RSS, yfinance,
  Finnhub) i jakości lokalnego modelu LLM — traktuj go jako dodatkowy
  kontekst, nie precyzyjny pomiar.
- `yfinance` korzysta z nieoficjalnego, publicznego API Yahoo Finance —
  bywa czasem niestabilne; narzędzie ma wbudowane ponawianie prób.
- Kontekst makro nie obejmuje inflacji (CPI) — brak w pełni darmowego,
  aktualnego źródła przez `yfinance`.

---

## 8. Struktura projektu

```
xtb_trend_watch/
├── config.example.yaml       # szablon konfiguracji (bez sekretów) - skopiuj jako config.yaml
├── config.yaml                # Twoja konfiguracja (w .gitignore, zawiera klucze API)
├── requirements.txt
├── README.md
├── run_daily.bat               # pomocniczy skrypt do Harmonogramu zadań Windows (tryb CLI)
├── data/                       # SQLite (watchlista, portfel, cache) - w .gitignore
├── reports/                    # raporty z CLI - w .gitignore
├── logs/                       # logi z run_daily.bat - w .gitignore
├── web/
│   └── static/                 # frontend dashboardu (HTML/CSS/JS, bez build-stepu)
│       ├── index.html
│       ├── style.css
│       └── app.js
└── src/
    ├── market_data.py          # ceny (yfinance) + cache w pamięci + benchmark
    ├── technical_analysis.py   # RSI/SMA/Bollinger/ATR/sharp_decline/siła względna
    ├── fundamentals.py         # P/E, wzrost, marże, cena docelowa, typ instrumentu (ETF/akcja)
    ├── macro_context.py        # VIX, rentowność obligacji - filtr ryzyka
    ├── fx_rates.py             # kursy walut: bieżące, historyczne, oficjalne NBP
    ├── news_sources.py         # newsy bieżące (yfinance) + RSS makro
    ├── news_history.py         # newsy historyczne (Finnhub) - cache przyrostowy
    ├── llm_sentiment.py        # sentyment przez Ollama + fallback słownikowy
    ├── discovery.py            # LLM proponuje nowe spółki do obserwacji
    ├── daily_brief.py          # codzienny brief AI (podsumowanie cyklu)
    ├── chatbot.py              # interaktywny asystent czatu (Ollama)
    ├── backtest.py             # backtest + grid search + walidacja walk-forward
    ├── collect_training_data.py       # (badawcze) zbieranie danych do ew. fine-tuningu
    ├── calibration_report.py          # (badawcze) kalibracja sentymentu LLM
    ├── daily_aggregate_calibration.py # (badawcze) jw., sentyment zagregowany dziennie
    ├── backfill_history.py     # symulacja przeszłych cykli technicznych na historii cen
    ├── report.py                # scoring, kategorie, ryzyko portfela, podatki, koncentracja sektorowa
    ├── db.py                    # SQLite: watchlist, portfolio, cache, historia, skuteczność
    ├── analysis_engine.py       # wspólny silnik analizy (CLI + dashboard)
    ├── web_app.py                # serwer FastAPI: REST API + WebSocket + harmonogramy w tle
    └── main.py                  # punkt wejścia CLI

```

Pełny opis wszystkich funkcji dashboardu znajdziesz w
`XTB_Trend_Watch_Instrukcja_Uzytkownika.pdf`.

---

## 9. Możliwe dalsze rozszerzenia

- Eksport/import watchlisty i portfela (CSV/JSON).
- Alternatywne źródła newsów dla spółek spoza głównych giełd US (Finnhub
  zwraca 403 dla większości spółek z GPW) — obecnie świadomie pominięte
  jako zbyt kruche (scrapowanie) względem korzyści.
- Rozszerzenie panelu ryzyka o zmienność portfela i wskaźnik Sharpe’a.
- Krzywa kapitału portfela na tle benchmarku rynkowego w tym samym okresie.
