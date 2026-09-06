# XTB Trend Watch

Narzędzie do **codziennej analizy** wybranych spółek (globalnie) pod kątem:
- wskaźników technicznych na **dwóch interwałach** (dzienny + tygodniowy jako
  potwierdzenie), z wykrywaniem "górek" (RSI, dystans od 52-tyg. maksimum,
  wstęgi Bollingera, wolumen),
- sentymentu newsów (bieżące + trend 12-miesięczny, per spółka + ogólnorynkowe RSS),
- podstawowej analizy fundamentalnej (P/E, wzrost przychodów, marże, zadłużenie),
- szerokiego kontekstu makro (VIX, rentowność obligacji) jako filtra ryzyka,

i sugerujące, kiedy warunki wyglądają na **rozsądny punkt wejścia**, a kiedy
lepiej **poczekać, bo cena jest blisko "górki"** trendu. Dodatkowo LLM może
**samodzielnie proponować nowe spółki** do obserwacji, a osobny skrypt
pozwala **przetestować strategię wejścia na danych historycznych** (backtest).

> ⚠️ **To nie jest system automatycznego handlu ani porada inwestycyjna.**
> Narzędzie generuje raport (plik `.md` i `.json`), który czytasz i na jego
> podstawie **ręcznie** podejmujesz decyzję oraz składasz zlecenie w XTB.
> Nie łączy się z Twoim kontem XTB i nie handluje za Ciebie.

---

## 1. Jak to działa (skrót logiki)

Dla każdej spółki z `config.yaml`:

1. Pobiera ~2 lata danych cenowych (Yahoo Finance przez `yfinance`).
2. Liczy wskaźniki na interwale **dziennym** i **automatycznie oznacza
   spółkę jako "AT_TOP"**, jeśli:
   - cena jest bardzo blisko 52-tygodniowego maksimum, **lub**
   - RSI wskazuje wykupienie (domyślnie ≥ 70).

   **Spółka oznaczona jako "AT_TOP" nigdy nie trafi do sugestii kupna**,
   niezależnie od tego, jak dobre są newsy czy fundamenty - to Twoje
   zabezpieczenie przed kupowaniem na szczycie.
3. Premiuje sytuacje klasycznego "buy the dip": trend wzrostowy (SMA50 > SMA200)
   + widoczne cofnięcie ceny od lokalnego szczytu.
4. **Potwierdza sygnał dzienny interwałem tygodniowym** - jeśli dzienne
   odbicie nie jest wsparte trendem wzrostowym na tygodniowym, sygnał jest
   obniżany (ochrona przed "pułapką byka").
5. Pobiera świeże newsy o spółce, ogólnorynkowe nagłówki makro oraz (opcjonalnie)
   newsy historyczne z ostatniego roku (Finnhub) - do wyliczenia trendu sentymentu.
6. Ocenia sentyment newsów - przez lokalny LLM (zalecane) lub prosty
   analizator słownikowy jako fallback offline.
7. Pobiera podstawowe wskaźniki fundamentalne (P/E, wzrost przychodów, marże,
   zadłużenie) i oznacza flagi typu "wysoka wycena bez wzrostu".
8. Sprawdza szeroki kontekst makro (VIX, rentowność obligacji) - w dniach
   podwyższonego ryzyka rynkowego **podnosi próg** wymagany do sugestii kupna.
9. Łączy wyniki (technika/sentyment/fundamenty, wagi konfigurowalne) w finalny
   score i przypisuje kategorię: `WARTO OBSERWOWAĆ`, `NEUTRALNIE`, `BRAK SYGNAŁU`
   lub `UNIKAJ - blisko szczytu`.
10. Opcjonalnie: LLM proponuje nowe spółki do obserwacji na bazie newsów makro
    (patrz sekcja 4b) - przechodzą przez dokładnie tę samą analizę.
11. Zapisuje raport do `reports/report_<data>.md` oraz `.json`.

Wagi, progi i watchlistę zmieniasz w `config.yaml` bez dotykania kodu.

---

## 2. Instalacja

```bash
cd xtb_trend_watch
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Uruchomienie

```bash
python -m src.main --config config.yaml
```

Raport pojawi się w terminalu (tabela) oraz w katalogu `reports/`.

### Uruchamianie codziennie automatycznie

**Linux/macOS (cron)** - np. codziennie o 8:00, przed otwarciem rynków europejskich:
```bash
crontab -e
# dodaj linię:
0 8 * * 1-5 cd /pełna/ścieżka/xtb_trend_watch && venv/bin/python -m src.main >> logs/cron.log 2>&1
```

**Windows (Harmonogram zadań) - najprościej przez dołączony `run_daily.bat`:**

1. Utwórz w folderze projektu podkatalog `logs` (jednorazowo): `mkdir logs`
2. Otwórz Harmonogram zadań (Win+S → "Harmonogram zadań")
3. Utwórz zadanie podstawowe:
   - Wyzwalacz: codziennie, np. 8:00
   - Akcja: uruchom program
     - Program/skrypt: `C:\xtb_trend_watch\run_daily.bat`
     - Rozpocznij w (opcja "Start in"): `C:\xtb_trend_watch`
4. Gotowe - `run_daily.bat` sam aktywuje `venv` i uruchamia skrypt, a wynik (łącznie
   z ewentualnymi błędami) trafia do `logs\daily_run.log`, więc możesz sprawdzić,
   czy wczorajsze uruchomienie się powiodło, bez czekania na kolejne okienko konsoli.

Jeśli wolisz skonfigurować to ręcznie bez `.bat`, ustaw bezpośrednio:
- Program: `C:\xtb_trend_watch\venv\Scripts\python.exe`
- Argumenty: `-m src.main --config config.yaml`
- Katalog roboczy: `C:\xtb_trend_watch`

---

## 3a. Dashboard webowy (zalecany sposób korzystania)

Zamiast odpalać skrypt CLI ręcznie/przez harmonogram, możesz uruchomić
**pełnoprawny dashboard webowy**, który sam, w tle, cyklicznie analizuje
watchlistę, pokazuje wyniki na żywo w przeglądarce i pozwala dodawać/usuwać
spółki bez dotykania `config.yaml`.

```bash
python -m src.web_app --config config.yaml
```

Konsola pokaże:
```
>>> Dashboard dostępny pod adresem: http://127.0.0.1:8000
```

Otwórz ten adres w przeglądarce. Serwer musi działać cały czas, żeby
dashboard się odświeżał - zostaw okno konsoli otwarte (albo, na Windows,
skonfiguruj go jako zadanie w tle - patrz niżej).

**Co widzisz na dashboardzie:**
- **Panel makro** (lewa góra) - reżim ryzyka (VIX, rentowność obligacji),
  aktualizowany w każdym cyklu.
- **Watchlista** - dodawanie nowej spółki (ticker + nazwa, opcjonalnie symbol
  XTB) i usuwanie istniejących, bez restartu serwera. Zmiana trafi do
  analizy w **następnym** cyklu.
- **Log na żywo** - dokładnie to, co wcześniej widziałeś w konsoli (postęp
  analizy krok po kroku), teraz strumieniowane przez WebSocket.
- **Wyniki** - karty spółek z "pieczęcią" kategorii (WARTO OBSERWOWAĆ /
  NEUTRALNIE / CZEKAJ / UNIKAJ), sygnałem technicznym, wynikiem i trendem
  sentymentu. **Kliknięcie karty otwiera wykres świecowy** (dane pobrane na
  żywo z Yahoo Finance) z naniesionymi średnimi SMA50/SMA200.
- **Propozycje AI** - osobna sekcja na kandydatów zaproponowanych przez LLM
  (dokładnie ta sama logika co w CLI, patrz sekcja 4b).
- **Przycisk "Odśwież teraz"** - wymusza natychmiastowy cykl analizy, nie
  czekając na harmonogram.

**Częstotliwość automatycznego odświeżania** ustawiasz w `config.yaml`:
```yaml
web:
  host: "127.0.0.1"
  port: 8000
  refresh_interval_minutes: 60     # co ile minut serwer sam analizuje watchlistę
  run_analysis_on_startup: true    # czy odpalić pierwszy cykl od razu po starcie
```

**Dane trwałe:** watchlista i cache ostatnich wyników są w
`data/xtb_trend_watch.db` (SQLite) - przetrwają restart serwera. Watchlista
z `config.yaml` jest używana tylko RAZ, do wstępnego zasilenia bazy przy jej
pierwszym utworzeniu - później to baza jest źródłem prawdy, a nie plik YAML.

**Uruchamianie dashboardu w tle na Windows** (żeby nie trzymać otwartego
okna konsoli): najprościej przez Harmonogram zadań, analogicznie do
`run_daily.bat` (sekcja 3), ale z wyzwalaczem "przy starcie systemu" zamiast
"codziennie", i akcją wskazującą na `python -m src.web_app` zamiast
`src.main`. Alternatywnie możesz użyć NSSM (Non-Sucking Service Manager) do
zarejestrowania go jako usługi Windows - to jednak wykracza poza podstawowy
zakres tego README; daj znać, jeśli chcesz, żebym to rozpisał krok po kroku.

---

## 4. Konfiguracja lokalnego LLM (Ollama) - zalecane

Do sensownej analizy sentymentu newsów **znacznie lepiej sprawdzi się lokalny
LLM** niż prosty słownik słów kluczowych. Polecany, prosty w konfiguracji
sposób to **Ollama**.

### 4.1 Instalacja Ollama

- **Linux**: `curl -fsSL https://ollama.com/install.sh | sh`
- **macOS**: pobierz instalator z https://ollama.com/download
- **Windows**: pobierz instalator z https://ollama.com/download

### 4.2 Pobranie modelu

Do analizy sentymentu newsów finansowych w rozsądnym czasie na zwykłym
komputerze (bez GPU serwerowego) polecane są modele w klasie 7-8B:

```bash
ollama pull llama3.1:8b
# alternatywy: mistral:7b, qwen2.5:7b (dobrze radzi sobie też z PL)
```

Jeśli masz mocniejszy sprzęt (np. 24GB+ VRAM), możesz użyć większego modelu
(np. `llama3.1:70b` lub `qwen2.5:32b`) dla dokładniejszej analizy - wystarczy
zmienić `llm.model` w `config.yaml`.

### 4.3 Uruchomienie serwera Ollama

```bash
ollama serve
```

Domyślnie nasłuchuje na `http://localhost:11434` - to jest wartość domyślna
w `config.yaml` (`llm.base_url`), więc nic więcej nie musisz zmieniać.

### 4.4 Test, czy działa

```bash
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.1:8b",
  "prompt": "Odpowiedz jednym słowem: czy 2+2=4?",
  "stream": false
}'
```

Jeśli dostaniesz odpowiedź w JSON-ie, wszystko działa i skrypt automatycznie
zacznie korzystać z LLM (bo `llm.enabled: true` w configu).

### 4.5 Jeśli nie chcesz stawiać LLM

Ustaw w `config.yaml`:
```yaml
llm:
  enabled: false
  fallback_to_lexicon: true
```
Skrypt będzie wtedy działał w 100% offline, korzystając z prostego
analizatora słów kluczowych (mniej trafny, ale zawsze dostępny).

---

## 4a. Newsy historyczne (do roku wstecz) - Finnhub

Domyślne źródła newsów (`yfinance` + RSS) dają tylko "migawkę" ostatnich
nagłówków - nie pozwalają zajrzeć np. 8 miesięcy wstecz. Jeśli chcesz, żeby
narzędzie **oceniało też, jak zmieniał się sentyment wokół spółki na
przestrzeni ostatniego roku** (a nie tylko "co się dzieje teraz"), potrzebny
jest darmowy klucz do [Finnhub](https://finnhub.io/register):

1. Zarejestruj się na https://finnhub.io/register (e-mail + hasło, ok. 2 min).
2. Po zalogowaniu skopiuj **API key** z panelu (Dashboard).
3. Wklej go w `config.yaml`:
   ```yaml
   news:
     use_historical_news: true
     finnhub_api_key: "tu_wklej_swoj_klucz"
     historical_lookback_days: 365
     max_headlines_per_month: 4
   ```

**Jak to działa:** dla każdej spółki skrypt pobiera nagłówki newsów w blokach
miesięcznych z ostatnich 12 miesięcy, liczy dla każdego miesiąca prosty
wynik sentymentu (analiza słownikowa - działa zawsze, offline), a jeśli
lokalny LLM jest włączony, dodatkowo generuje jedno zwięzłe podsumowanie
narracyjne całego roku (kluczowe wydarzenia, ewolucja tonu newsów).

Wynik trafia do raportu jako:
- tabela miesięcznych wyników sentymentu,
- etykieta trendu: `POPRAWIA_SIE` / `POGARSZA_SIE` / `STABILNY`,
- niewielka korekta finalnego wyniku (±0.05) - **celowo mała**, żeby nie
  zdominować analizy technicznej. Co ważne: **nawet bardzo pozytywny trend
  sentymentu nie odblokuje spółki oznaczonej jako "AT_TOP"** - zasada
  "nie kupuj na górce" zawsze ma pierwszeństwo.

**Limity darmowego planu Finnhub:** ok. 60 zapytań/minutę, co przy 14
spółkach × ~12 zapytań miesięcznych (jedno na miesiąc) daje ~168 zapytań na
pełen przebieg skryptu - w kodzie jest wbudowane opóźnienie między
zapytaniami, żeby nie przekroczyć limitu. Pierwsze uruchomienie z pełną
historią 12-miesięczną może więc potrwać kilka minut dłużej niż zwykle.

---

## 4b. Automatyczne odkrywanie nowych spółek (LLM proponuje kandydatów)

Oprócz analizy stałej watchlisty z `config.yaml`, skrypt może **codziennie
prosić lokalny LLM o zaproponowanie nowych spółek** wartych obserwacji - na
podstawie tego, co akurat dzieje się w newsach makro (trendy, tematy,
branże, które ostatnio wracają w nagłówkach).

Włączone domyślnie w `config.yaml`:
```yaml
discovery:
  enabled: true
  max_candidates: 5
  run_full_analysis_on_candidates: true
```

**Jak to działa:**
1. LLM dostaje bieżące nagłówki makro + listę spółek już obserwowanych (żeby
   nie proponować duplikatów) i zwraca do 5 kandydatów z uzasadnieniem.
2. **Każdy kandydat jest weryfikowany** - skrypt próbuje pobrać dla niego
   realne dane cenowe (yfinance). Jeśli się nie uda (bo np. LLM podał
   nieistniejący lub przestarzały ticker), kandydat jest odrzucany z
   wyraźnym komunikatem w konsoli.
3. Zweryfikowani kandydaci przechodzą przez **dokładnie tę samą** analizę
   techniczną i newsową co spółki ze stałej watchlisty - łącznie z zasadą
   "nie kupuj na górce" (hard block przy sygnale `AT_TOP`).
4. Wynik trafia do osobnej sekcji raportu: "Nowo odkryte spółki (propozycje AI)",
   wyraźnie oznaczonej jako wymagającej dodatkowej weryfikacji.

**Ważne ograniczenie:** to funkcja z natury "kreatywna" - LLM czasem może
zaproponować spółkę nietrafnie, z błędnym uzasadnieniem, albo pominąć coś
oczywistego. Traktuj te propozycje jako **punkt wyjścia do własnego
researchu**, a nie gotową listę do kupienia. Jeśli jakiś kandydat regularnie
Cię zainteresuje, rozważ dopisanie go na stałe do `watchlist` w
`config.yaml`, żeby zyskał też historię trendu sentymentu (Finnhub) z kolejnych dni.

Jeśli wolisz mieć tylko stałą, ręcznie kontrolowaną listę spółek (bez
propozycji AI), ustaw `discovery.enabled: false`.

---

## 4c. Analiza fundamentalna (P/E, wzrost przychodów, marże, zadłużenie)

Domyślnie włączona (`fundamentals.enabled: true`). Dla każdej spółki skrypt
pobiera z `yfinance` (`Ticker.info`) podstawowe wskaźniki:

- **P/E (trailing i forward)** - cena do zysku,
- **P/B** - cena do wartości księgowej,
- **wzrost przychodów i zysków rok do roku**,
- **marża netto**,
- **zadłużenie do kapitału własnego (D/E)**,
- **ROE, stopa dywidendy, kapitalizacja rynkowa**.

Na tej podstawie generowane są proste, regułowe flagi (np. "wysoka wycena
przy niskim wzroście przychodów", "malejące przychody", "wysokie
zadłużenie"). Wynik fundamentalny ma **wagę 15%** w finalnym wyniku
(`scoring.fundamentals_weight`) - jeśli dane fundamentalne są niedostępne
dla danej spółki (zdarza się to czasem dla mniejszych spółek spoza USA),
waga jest automatycznie przenoszona z powrotem na analizę techniczną
i sentyment, więc brak danych fundamentalnych nie zaniża sztucznie wyniku.

To NIE jest pełna wycena spółki (DCF, porównanie do sektora itp.) - to
szybki, ogólnodostępny kontekst, traktowany jako dodatkowy głos w łącznej
ocenie, nie jako wyrocznia.

---

## 4d. Szeroki kontekst makro (VIX, rentowność obligacji) - filtr ryzyka

Domyślnie włączony (`macro.enabled: true`). Raz na uruchomienie skrypt
pobiera:
- **VIX** (indeks zmienności S&P 500, tzw. "indeks strachu"),
- **rentowność 10-letnich obligacji skarbowych USA** (`^TNX`).

Na tej podstawie klasyfikuje bieżący "reżim ryzyka" rynkowego jako
`SPOKOJNY`, `PODWYZSZONY` lub `WYSOKI`. W dniach podwyższonego ryzyka
**próg wymagany do sugestii kupna jest automatycznie podnoszony**
(domyślnie +0.05 przy VIX ≥ 20, +0.10 przy VIX ≥ 30) - czyli w niepewnym
otoczeniu rynkowym sugestie "WARTO OBSERWOWAĆ" pojawiają się rzadziej i
tylko przy naprawdę mocnych sygnałach. To globalny filtr dla całego
raportu, widoczny w jego nagłówku.

Świadomie pominięta jest inflacja (CPI) - nie ma do niej w pełni darmowego,
aktualnego źródła przez `yfinance`; VIX i rentowność obligacji są sensownym,
w pełni darmowym substytutem szerokiego kontekstu makro.

---

## 4e. Potwierdzenie wielointerwałowe (dzienny + tygodniowy)

Domyślnie włączone (`technical.use_weekly_confirmation: true`). Klasyczna
zasada tradingu: **handluj zgodnie z trendem wyższego interwału**. Skrypt
liczy te same wskaźniki (SMA, RSI) również na świecach **tygodniowych**
i wymaga potwierdzenia:

- Jeśli sygnał dzienny to `GOOD_ENTRY`, ale trend tygodniowy NIE jest
  wzrostowy (lub RSI tygodniowy jest wykupiony) → sygnał zostaje obniżony
  do `NEUTRAL`. To ochrona przed klasyczną **"pułapką byka"** - odbiciem na
  dziennym w ramach szerszego trendu spadkowego.
- Jeśli trend tygodniowy potwierdza sygnał dzienny → niewielka premia do
  wyniku (wyższa wiarygodność).

Okresy średnich na tygodniowym są konfigurowalne osobno
(`weekly_ma_short`/`weekly_ma_long`, domyślnie 10/40 tygodni - odpowiednik
SMA50/SMA200 na dziennym).

---

## 4f. Backtesting - "co by było, gdyby" na danych historycznych

Osobny, samodzielny skrypt (`src/backtest.py`) do sprawdzenia, jak logika
techniczna (RSI/SMA/Bollinger/52w-high, bez komponentu newsowego) radziła
sobie w przeszłości dla konkretnej spółki:

```bash
python -m src.backtest --ticker MSFT --years 5
python -m src.backtest --ticker ALE.WA --years 3 --hold-days 15
```

Parametry:
- `--ticker` - ticker Yahoo Finance (wymagany),
- `--years` - ile lat historii testować (domyślnie 5),
- `--hold-days` - maks. liczba dni trzymania pozycji, jeśli wcześniej nie
  pojawi się sygnał `AT_TOP` (domyślnie 20),
- `--config` - ścieżka do configu (domyślnie `config.yaml` - stamtąd biorą
  się parametry wskaźników technicznych).

**Jak działa symulacja:** strategia "wchodzi" na zamknięciu dnia, w którym
pojawia się sygnał `GOOD_ENTRY` (identyczna logika co w analizie na żywo -
trend wzrostowy + cofnięcie od lokalnego szczytu + brak wykupienia), i
"wychodzi" albo po `--hold-days` dniach, albo wcześniej, jeśli pojawi się
sygnał `AT_TOP` (swego rodzaju wzięcie zysku). Wynik: liczba transakcji,
win rate, średni zwrot, zwrot skumulowany strategii **vs. Buy & Hold** w
tym samym okresie.

**Ważne ograniczenia tego backtestu** (naprawdę warto je znać, zanim
uwierzysz w wynik):
- Testuje WYŁĄCZNIE logikę techniczną - bez newsów/sentymentu/fundamentów
  (nie mamy darmowego, historycznego newsfeedu zsynchronizowanego
  dzień-po-dniu na potrzeby pełnego backtestu).
- Nie modeluje prowizji, spreadu, poślizgu cenowego (slippage) ani podatku
  od zysków kapitałowych - realny wynik "na żywo" będzie niższy.
- Kilka lat historii jednej spółki to wciąż mała próbka statystyczna, mocno
  zależna od tego, czy okres akurat obejmował hossę, czy bessę - traktuj
  wynik jako punkt wyjścia do zrozumienia zachowania strategii, nie jako
  obietnicę przyszłego zwrotu.

---

## 5. Mapowanie spółek na symbole XTB

W `config.yaml` każda spółka ma dwa pola:
```yaml
- ticker: "AAPL"          # symbol używany do pobierania danych (Yahoo Finance)
  xtb_symbol: "AAPL.US"   # jak szukać tego instrumentu w aplikacji XTB
  name: "Apple Inc."
```
Yahoo Finance i XTB czasem różnie nazywają te same instrumenty (szczególnie
dla spółek spoza USA, np. giełda w Warszawie, Frankfurcie czy Tokio) -
`xtb_symbol` to tylko Twoja notatka pomocnicza, żebyś wiedział, czego szukać
w XTB. Warto zweryfikować dokładną nazwę bezpośrednio w aplikacji XTB, bo
symbole mogą się zmieniać.

Dodawanie nowej spółki do obserwacji = dopisanie kolejnego wpisu do listy
`watchlist` w `config.yaml`. Tickery Yahoo Finance dla rynków spoza USA mają
zwykle sufiks giełdy, np.:
- `.WA` - Giełda Warszawska
- `.DE` - Frankfurt (Xetra)
- `.AS` - Amsterdam (Euronext)
- `.L` - Londyn
- `.T` - Tokio
- `.PA` - Paryż

---

## 6. Ograniczenia i pomysły na rozbudowę

**Ograniczenia obecnej wersji:**
- Analiza fundamentalna to szybki, regułowy zestaw wskaźników (P/E, wzrost
  przychodów, marże, zadłużenie) - nie zastępuje pełnej wyceny spółki (DCF,
  porównania sektorowego, analizy sprawozdań finansowych).
- Backtest (`src/backtest.py`) testuje wyłącznie logikę techniczną, bez
  newsów/sentymentu/fundamentów, i nie modeluje prowizji/spreadu/podatku -
  patrz szczegółowe zastrzeżenia w sekcji 4f.
- Sentyment newsów zależy od jakości i dostępności kanałów RSS oraz newsów
  z `yfinance`/Finnhub - w razie problemów z konkretnym źródłem, po prostu
  pomija je i loguje ostrzeżenie.
- `yfinance` korzysta z nieoficjalnego, publicznego API Yahoo Finance - bywa
  czasem niestabilne; w kodzie jest prosty mechanizm ponawiania prób.
- Kontekst makro (VIX, rentowność obligacji) nie obejmuje inflacji (CPI) -
  brak w pełni darmowego, aktualnego źródła przez `yfinance`.

**Możliwe dalsze rozszerzenia (jeśli chcesz, mogę je dopisać):**
- Alert e-mail/Telegram, gdy pojawi się nowa spółka w kategorii
  "WARTO OBSERWOWAĆ".
- Backtest uwzględniający też prosty model kosztów transakcyjnych (prowizja
  XTB + przybliżony spread) dla bardziej realistycznych wyników.
- Automatyczne uruchamianie backtestu dla całej watchlisty naraz (obecnie:
  jedna spółka na wywołanie skryptu) i zapis wyników do zbiorczego raportu.
- Analiza sektorowa/korelacji między spółkami z watchlisty (np. ostrzeżenie
  o nadmiernej koncentracji w jednej branży).

---

## 7. Struktura projektu

```
xtb_trend_watch/
├── config.yaml              # watchlista (seed), progi, wagi, LLM, Finnhub, discovery, fundamentals, macro, web
├── requirements.txt
├── README.md
├── run_daily.bat             # pomocniczy skrypt do Harmonogramu zadań Windows (tryb CLI)
├── data/                     # SQLite (watchlista + cache wyników dashboardu) - tworzone automatycznie
├── reports/                 # raporty z CLI (python -m src.main) - tworzone automatycznie
├── logs/                    # logi z uruchomień przez run_daily.bat - tworzone automatycznie
├── web/
│   └── static/                # frontend dashboardu (HTML/CSS/JS, bez build-stepu)
│       ├── index.html
│       ├── style.css
│       └── app.js
└── src/
    ├── market_data.py       # pobieranie danych cenowych (yfinance)
    ├── technical_analysis.py# wskaźniki dzienne+tygodniowe + logika "unikaj górek"
    ├── fundamentals.py       # analiza fundamentalna (P/E, wzrost, marże, zadłużenie)
    ├── macro_context.py      # szeroki kontekst makro (VIX, rentowność obligacji) - filtr ryzyka
    ├── news_sources.py      # newsy per-spółka (yfinance) + RSS makro
    ├── news_history.py      # newsy historyczne do roku wstecz (Finnhub)
    ├── llm_sentiment.py     # sentyment bieżący + trend 12-miesięczny: lokalny LLM + fallback
    ├── discovery.py          # LLM proponuje nowe spółki do obserwacji na bazie newsów makro
    ├── backtest.py            # samodzielny skrypt: backtest strategii na danych historycznych
    ├── report.py             # łączenie wyników, generowanie raportu Markdown/JSON (tryb CLI)
    ├── db.py                  # SQLite: watchlista (CRUD) + cache wyników (tryb dashboard)
    ├── analysis_engine.py    # wspólny silnik analizy (reużywany przez main.py i web_app.py)
    ├── web_app.py             # serwer FastAPI: REST API + WebSocket + harmonogram w tle
    └── main.py                # punkt wejścia CLI (cienka warstwa nad analysis_engine)
```
