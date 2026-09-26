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
- Ostrzeżenia o zbliżających się wynikach kwartalnych (badge na kartach,
  baner w panelu spółki, panel „Nadchodzące wyniki”, uwaga przy pozycjach
  w portfelu, wzmianka w briefie dnia i w czacie). Czysto informacyjne —
  nie zmieniają wyniku ani kategorii.
- Automatyczna blokada sugestii kupna blisko szczytu trendu lub tuż po
  gwałtownym spadku ceny — z osobną, łagodniejszą logiką dla ETF-ów.
- Siła względna wobec benchmarku z automatycznym zastępczym źródłem danych
  (np. Stooq dla ^WIG20), gdy główny benchmark jest niedostępny w Yahoo
  Finance — bez tego cała watchlista GPW traciłaby ten sygnał, a każda
  spółka marnowałaby czas na powtarzane, nieudane zapytania.
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
- Własny stop-loss i własny cel cenowy per pozycja (opcjonalne): nadpisują
  sugestię z ATR w rekomendacji, w panelu ryzyka i w alertach cenowych.
- Panel „Korelacja i zmienność”: roczna zmienność, Sharpe, maks. obsunięcie,
  macierz korelacji i współczynnik dywersyfikacji dla obecnych wag pozycji —
  pokazuje, czy kilka spółek to w praktyce jeden zakład (np. sektor AI/tech).
- Porównanie portfela z benchmarkiem (SPY / WIG20 / własny): zwrot, beta,
  alfa, wychwyt wzrostów i spadków oraz wykres — odpowiada na pytanie, czy
  wynik to selekcja spółek, czy po prostu ekspozycja na rynek.
- Import pozycji z raportu XTB (.xlsx: „Open Positions” i „Closed
  Positions”) — idempotentny, mapuje symbole XTB na tickery Yahoo Finance.
- Kopia zapasowa watchlisty i portfela (eksport/import JSON, idempotentny).
- Alerty cenowe niezależne od pełnego cyklu (sprawdzanie samej ceny co
  kilka minut, bez angażowania LLM/newsów): poniżej stop-lossu (własnego
  albo z ATR), osiągnięcie ceny docelowej analityków i własnego celu.
  Opcjonalnie wysyłane też na Discorda (webhook, sekcja 6) — niezależnie
  od tego, czy dashboard jest akurat otwarty.

**Dashboard i asystent:**
- Pełny dashboard webowy (FastAPI + WebSocket) z trzema zakładkami
  (Analiza, Portfel, Zamknięte transakcje), watchlistą, miniwykresami,
  panelem skuteczności narzędzia i zwijanym logiem na żywo.
- Personalizacja interfejsu (zapamiętywana lokalnie w przeglądarce): panele
  boczne zakładki Analiza można zwijać i dowolnie przestawiać (▲/▼), a
  widoczne kolumny watchlisty/propozycji AI (cena docelowa, sygnał, wynik,
  wykres) włącza się i wyłącza przyciskiem „⚙ Kolumny”.
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
w sekcji `web:` pliku `config.yaml`. Host i port możesz też nadpisać z linii
poleceń: `--host` i `--port`.

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

W `config.example.yaml` `use_historical_news` jest domyślnie **wyłączone** —
włącz je (`true`) dopiero po wpisaniu prawdziwego klucza; z kluczem-zaślepką
każdy cykl marnuje czas na nieudane zapytania.

Jeśli nie chcesz newsów historycznych, ustaw `news.use_historical_news: false`
— narzędzie będzie działać w pełni offline (poza pobieraniem cen), korzystając
z prostego analizatora słownikowego.

---

## 6. Powiadomienia Discord o alertach cenowych (opcjonalnie)

Alerty cenowe (przebicie stop-lossu, osiągnięcie celu) zawsze trafiają do
logu na żywo w dashboardzie — ale to działa tylko wtedy, gdy masz otwartą
kartę przeglądarki. Żeby dostać powiadomienie także wtedy, gdy dashboard
jest zamknięty, można podpiąć webhook Discorda:

1. Na serwerze Discord: **Ustawienia serwera → Integracje → Webhooki →
   Nowy webhook**, skopiuj URL (nie trzeba zakładać bota ani niczego
   autoryzować z poziomu tej aplikacji).
2. W `config.yaml`:
   ```yaml
   notifications:
     enabled: true
     discord_webhook_url: "wklejony-url-webhooka"
   ```
3. Zrestartuj serwer dashboardu.

W zakładce Portfel → "Ryzyko i ekspozycja" jest przycisk **"🧪 Testuj
Discord"** — wysyła jedną testową wiadomość, żeby od razu sprawdzić, czy
webhook działa, bez czekania na prawdziwy alert. Domyślnie (`enabled: false`)
funkcja jest wyłączona i nic się nie wysyła.

---

## 7. Mapowanie spółek na symbole brokera

W `config.yaml` każda spółka ma dwa pola:
```yaml
- ticker: "AAPL"          # symbol do pobierania danych (Yahoo Finance)
  xtb_symbol: "AAPL.US"   # notatka pomocnicza - jak szukać instrumentu u brokera
  name: "Apple Inc."
```
Typowe sufiksy giełd dla tickerów spoza USA: `.WA` (Warszawa), `.DE`
(Frankfurt), `.AS` (Amsterdam), `.L` (Londyn), `.T` (Tokio), `.PA` (Paryż).

---

## 8. Ograniczenia (przeczytaj, zanim zaufasz wynikom)

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
  kontekst, nie precyzyjny pomiar. W teście na ok. 24 tys. historycznych
  nagłówków (dwie spółki) sentyment pojedynczego nagłówka nie wykazał
  związku z późniejszą zmianą ceny, więc nie jest sygnałem predykcyjnym.
- Daty wyników kwartalnych pochodzą z Yahoo Finance i bywają szacunkowe.
- Statystyki portfela (korelacje, zmienność, Sharpe) dotyczą hipotetycznego
  portfela o obecnych wagach na rocznej historii, w walutach notowania
  (bez wpływu kursów walut) — to obraz ryzyka, nie prognoza.
- Optymalizacja parametrów w backteście dotyczy wąskiej, silnie
  skorelowanej watchlisty w konkretnym reżimie rynkowym — nie generalizuj
  wyników na inne okresy bez własnej weryfikacji.
- `yfinance` korzysta z nieoficjalnego, publicznego API Yahoo Finance —
  bywa czasem niestabilne; narzędzie ma wbudowane ponawianie prób.
- Kontekst makro nie obejmuje inflacji (CPI) — brak w pełni darmowego,
  aktualnego źródła przez `yfinance`.

---

## 9. Struktura projektu

```
xtb_trend_watch/
├── config.example.yaml       # szablon konfiguracji (bez sekretów) - skopiuj jako config.yaml
├── config.yaml                # Twoja konfiguracja (w .gitignore, zawiera klucze API)
├── requirements.txt
├── README.md
├── XTB_Trend_Watch_Instrukcja_Uzytkownika.pdf   # instrukcja użytkownika (v3.0)
├── build_manual.py             # generator instrukcji PDF (reportlab)
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
    ├── fundamentals.py         # P/E, wzrost, marże, cena docelowa, typ instrumentu (ETF/akcja), termin wyników
    ├── macro_context.py        # VIX, rentowność obligacji - filtr ryzyka
    ├── fx_rates.py             # kursy walut: bieżące, historyczne, oficjalne NBP
    ├── notifications.py        # powiadomienia zewnętrzne o alertach cenowych (Discord webhook)
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
    ├── report.py                # scoring, kategorie, ryzyko i statystyki portfela (korelacje, Sharpe), podatki, koncentracja sektorowa
    ├── xtb_import.py            # import pozycji z raportu XTB (.xlsx)
    ├── json_utils.py            # sanityzacja NaN/Infinity przed serializacją JSON
    ├── db.py                    # SQLite: watchlist, portfolio, cache, historia, skuteczność, kopia zapasowa
    ├── analysis_engine.py       # wspólny silnik analizy (CLI + dashboard)
    ├── web_app.py                # serwer FastAPI: REST API (w tym kopia zapasowa, statystyki portfela) + WebSocket + harmonogramy w tle
    └── main.py                  # punkt wejścia CLI

```

Pełny opis wszystkich funkcji dashboardu znajdziesz w
`XTB_Trend_Watch_Instrukcja_Uzytkownika.pdf` (wersja 3.0). Instrukcję
generuje skrypt `build_manual.py` (`python build_manual.py`); wymaga
dodatkowo pakietów `reportlab` i `fonttools`, które **nie** są potrzebne do
działania samego narzędzia (nie ma ich w `requirements.txt`).

---

## 10. Możliwe dalsze rozszerzenia

- Prawdziwa (ważona czasem, TWR) krzywa kapitału na tle benchmarku —
  wymaga zapisywania przepływów gotówki (wpłat i wypłat), których snapshoty
  krzywej kapitału dziś nie zawierają. Obecne porównanie z benchmarkiem
  dotyczy hipotetycznego portfela o obecnych wagach.
- Sprawdzenie w backteście, czy okno tuż przed wynikami kwartalnymi
  pogarsza sygnały GOOD_ENTRY; jeśli tak, ostrzeżenie o wynikach mogłoby
  obniżać kategorię zamiast tylko informować.
- Alternatywne źródła newsów dla spółek spoza głównych giełd US (Finnhub
  zwraca 403 dla większości spółek z GPW) — obecnie świadomie pominięte
  jako zbyt kruche (scrapowanie) względem korzyści.
- Eksport historii zamkniętych transakcji do CSV (np. do arkusza z rozliczeniem
  podatkowym).
