"""
build_manual.py
-----------------
Generuje PDF „XTB Trend Watch — Instrukcja użytkownika” (reportlab + DejaVu Sans,
żeby poprawnie renderować polskie znaki).

Użycie:
    python build_manual.py                         # -> XTB_Trend_Watch_Instrukcja_Uzytkownika.pdf
    python build_manual.py --out inna_nazwa.pdf

Jak aktualizować dokument w przyszłości:
  1. Zmień treść w sekcji "TREŚĆ" (funkcje h1/h2/p/bullets/table/callout...).
  2. Podbij VERSION / DATE_LABEL oraz dopisz wiersz w rozdziale "Historia zmian".
  3. Uruchom skrypt - spis treści, numery stron i zakładki PDF liczą się same.

Uwaga: czcionka DejaVu NIE zawiera emoji. Skrypt sprawdza na końcu, czy wszystkie
użyte znaki istnieją w czcionce, i przerywa z listą brakujących - dzięki temu
w PDF nigdy nie pojawią się puste kwadraciki zamiast ikon. Przyciski z emoji
opisuj słownie (np. "przycisk z ikoną worka pieniędzy").
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as RLTTFont
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Frame, KeepTogether, ListFlowable, ListItem,
    NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

VERSION = "3.11"
DATE_LABEL = "wrzesień 2026"
DOC_TITLE = "XTB Trend Watch — Instrukcja użytkownika"

# ---------------------------------------------------------------- czcionki ---
def _find_font_dir() -> Path:
    """Szuka DejaVu Sans w typowych miejscach na Linuksie/Windows - skrypt był
    pierwotnie pisany na Linuksie (ścieżka na sztywno), więc na Windows (gdzie
    DejaVu bywa po prostu zainstalowane systemowo w C:\\Windows\\Fonts, tak jak
    inne czcionki TrueType) w ogóle się nie uruchamiał."""
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu"),          # Debian/Ubuntu
        Path("/usr/share/fonts/dejavu-sans-fonts"),          # Fedora/RHEL
        Path.home() / ".fonts",
        Path(r"C:\Windows\Fonts"),
    ]
    for c in candidates:
        if (c / "DejaVuSans.ttf").exists():
            return c
    raise FileNotFoundError(
        "Nie znaleziono czcionek DejaVu Sans. Zainstaluj pakiet fonts-dejavu-core "
        "(Linux) albo pobierz i zainstaluj DejaVu Sans z https://dejavu-fonts.github.io "
        "(Windows/macOS)."
    )


FONT_DIR = _find_font_dir()
_FONTS = {
    "DejaVu": "DejaVuSans.ttf",
    "DejaVu-Bold": "DejaVuSans-Bold.ttf",
    "DejaVu-Oblique": "DejaVuSans-Oblique.ttf",
    "DejaVu-BoldOblique": "DejaVuSans-BoldOblique.ttf",
    "DejaVuMono": "DejaVuSansMono.ttf",
    "DejaVuMono-Bold": "DejaVuSansMono-Bold.ttf",
}
for _name, _file in _FONTS.items():
    pdfmetrics.registerFont(RLTTFont(_name, str(FONT_DIR / _file)))
pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold",
                              italic="DejaVu-Oblique", boldItalic="DejaVu-BoldOblique")
pdfmetrics.registerFontFamily("DejaVuMono", normal="DejaVuMono", bold="DejaVuMono-Bold",
                              italic="DejaVuMono", boldItalic="DejaVuMono-Bold")

# ------------------------------------------------------------------ kolory ---
NAVY = colors.HexColor("#14213D")
GOLD = colors.HexColor("#9E7D20")
INK = colors.HexColor("#1B1F2A")
MUTED = colors.HexColor("#5C667F")
RULE = colors.HexColor("#D5D9E2")
ZEBRA = colors.HexColor("#F4F5F8")
RED = colors.HexColor("#B03A2E")
CHIP = {"buy": "#27814E", "watch": "#AD7721", "wait": "#5C667F", "avoid": "#AB452E"}
CALLOUT = {
    # rodzaj: (etykieta, kolor paska, kolor tła)
    "tip": ("WSKAZÓWKA", colors.HexColor("#9E7D20"), colors.HexColor("#FFF8E1")),
    "note": ("UWAGA", colors.HexColor("#9E7D20"), colors.HexColor("#FFF8E1")),
    "warn": ("OSTRZEŻENIE", colors.HexColor("#A67F22"), colors.HexColor("#F9EDE1")),
    "important": ("WAŻNE", colors.HexColor("#B03A2E"), colors.HexColor("#FBEDEA")),
}

PAGE_W, PAGE_H = A4
MARGIN_X = 20 * mm
CONTENT_W = PAGE_W - 2 * MARGIN_X

# ------------------------------------------------------------------- style ---
def _style(name, **kw):
    base = dict(fontName="DejaVu", fontSize=9.6, leading=14.2, textColor=INK, spaceAfter=5)
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "Body": _style("Body"),
    "H1": _style("H1", fontName="DejaVu-Bold", fontSize=19, leading=24, textColor=NAVY,
                 spaceBefore=16, spaceAfter=8),
    "H2": _style("H2", fontName="DejaVu-Bold", fontSize=12.2, leading=16, textColor=GOLD,
                 spaceBefore=10, spaceAfter=5),
    "Cell": _style("Cell", fontSize=8.7, leading=12, spaceAfter=0),
    "CellHead": _style("CellHead", fontName="DejaVu-Bold", fontSize=8.7, leading=12,
                       textColor=colors.white, spaceAfter=0),
    "CellMono": _style("CellMono", fontName="DejaVuMono", fontSize=7.8, leading=11, spaceAfter=0),
    "CalloutLabel": _style("CalloutLabel", fontSize=8.4, leading=11, textColor=GOLD, spaceAfter=3),
    "CalloutText": _style("CalloutText", fontSize=9.2, leading=13.4, spaceAfter=3),
    "Code": _style("Code", fontName="DejaVuMono", fontSize=8.3, leading=12, spaceAfter=0),
    "Chip": _style("Chip", fontName="DejaVu-Bold", fontSize=8.4, leading=11, textColor=colors.white,
                   alignment=TA_CENTER, spaceAfter=0),
    "TOC1": _style("TOC1", fontSize=10, leading=17, spaceAfter=0, leftIndent=0),
    "TOCTitle": _style("TOCTitle", fontName="DejaVu-Bold", fontSize=19, leading=24, textColor=NAVY,
                       spaceAfter=10),
    "CoverMark": _style("CoverMark", fontSize=30, leading=34, textColor=GOLD, alignment=TA_CENTER),
    "CoverTitle": _style("CoverTitle", fontName="DejaVu-Bold", fontSize=34, leading=40,
                         textColor=NAVY, alignment=TA_CENTER, spaceAfter=4),
    "CoverSub": _style("CoverSub", fontSize=17, leading=22, textColor=MUTED, alignment=TA_CENTER),
    "CoverMeta": _style("CoverMeta", fontName="DejaVuMono", fontSize=9.4, textColor=MUTED,
                        alignment=TA_CENTER),
    "CoverWarnHead": _style("CoverWarnHead", fontName="DejaVu-Bold", fontSize=11.5, textColor=RED,
                            alignment=TA_CENTER, spaceAfter=4),
    "CoverWarn": _style("CoverWarn", fontSize=9.6, leading=14.5, alignment=TA_CENTER),
}

# ------------------------------------------------ kontrola pokrycia czcionek ---
_ALL_TEXT: list[str] = []


def _reg(text: str) -> None:
    _ALL_TEXT.append(text)


def check_glyph_coverage() -> None:
    cmap = TTFont(str(FONT_DIR / "DejaVuSans.ttf")).getBestCmap()
    cmap_bold = TTFont(str(FONT_DIR / "DejaVuSans-Bold.ttf")).getBestCmap()
    cmap_mono = TTFont(str(FONT_DIR / "DejaVuSansMono.ttf")).getBestCmap()
    missing = set()
    for raw in _ALL_TEXT:
        text = re.sub(r"<[^>]+>", "", raw)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        for ch in text:
            if ch in "\n\t ":
                continue
            if not (ord(ch) in cmap and ord(ch) in cmap_bold and ord(ch) in cmap_mono):
                missing.add(ch)
    if missing:
        sys.exit("BRAK GLIFÓW w czcionce DejaVu: " + " ".join(f"{c} (U+{ord(c):04X})" for c in sorted(missing))
                 + "\nOpisz te ikony słownie zamiast używać emoji.")


# --------------------------------------------------------------- komponenty ---
def P(text: str, style: str = "Body") -> Paragraph:
    _reg(text)
    return Paragraph(text, S[style])


def c(text: str) -> str:
    """Kod/nazwa parametru w tekście akapitu (czcionka monospace)."""
    return f'<font name="DejaVuMono" size="8.4" color="#3A4256">{text}</font>'


class Story(list):
    """Lista flowables z wygodnymi metodami budowania treści."""

    def h1(self, text):
        self.append(CondPageBreak(60 * mm))
        self.append(P(text, "H1"))

    def h2(self, text):
        # CondPageBreak zamiast keepWithNext: keepWithNext przenosił cały nagłówek + kolejną
        # dużą tabelę na następną stronę i zostawiał puste pół strony.
        self.append(CondPageBreak(38 * mm))
        self.append(P(text, "H2"))

    def p(self, text):
        self.append(P(text))

    def bullets(self, items):
        self.append(ListFlowable(
            [ListItem(P(t), leftIndent=14, bulletColor=GOLD) for t in items],
            bulletType="bullet", start="•", leftIndent=14, bulletFontName="DejaVu",
            bulletFontSize=9, spaceAfter=4,
        ))

    def steps(self, items):
        self.append(ListFlowable(
            [ListItem(P(t), leftIndent=18) for t in items],
            bulletType="1", bulletFormat="%s.", leftIndent=18, bulletFontName="DejaVu-Bold",
            bulletFontSize=9, bulletColor=GOLD, spaceAfter=4,
        ))

    def code(self, text):
        _reg(text)
        cell = Paragraph(text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                         .replace(" ", "&nbsp;"), S["Code"])
        t = Table([[cell]], colWidths=[CONTENT_W])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EEF0F5")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        self.append(t)
        self.append(Spacer(1, 6))

    def callout(self, kind, *paragraphs):
        label, bar, bg = CALLOUT[kind]
        inner = [P(label, "CalloutLabel")] + [P(t, "CalloutText") for t in paragraphs]
        t = Table([["", inner]], colWidths=[3 * mm, CONTENT_W - 3 * mm - 3 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), bar), ("BACKGROUND", (1, 0), (1, 0), bg),
            ("LEFTPADDING", (1, 0), (1, 0), 10), ("RIGHTPADDING", (1, 0), (1, 0), 10),
            ("TOPPADDING", (1, 0), (1, 0), 7), ("BOTTOMPADDING", (1, 0), (1, 0), 5),
            ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ]))
        self.append(Spacer(1, 3))
        self.append(KeepTogether(t))
        self.append(Spacer(1, 8))

    def table(self, header, rows, widths, mono_first_col=False):
        """widths: udziały względne (sumują się do dowolnej wartości)."""
        total = float(sum(widths))
        col_w = [CONTENT_W * w / total for w in widths]
        data = [[P(h, "CellHead") for h in header]]
        for row in rows:
            data.append([P(cell, "CellMono" if (mono_first_col and i == 0) else "Cell")
                         for i, cell in enumerate(row)])
        t = Table(data, colWidths=col_w, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
            ("BOX", (0, 0), (-1, -1), 0.4, RULE),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ]
        for r in range(1, len(data)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
        t.setStyle(TableStyle(style))
        self.append(t)
        self.append(Spacer(1, 8))

    def chips(self, rows):
        """rows: (kolor_klucz, etykieta, opis) - kolorowa pieczęć + opis kategorii."""
        data = []
        style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                 ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
        for i, (key, label, desc) in enumerate(rows):
            data.append([P(label, "Chip"), P(desc, "Cell")])
            style.append(("BACKGROUND", (0, i), (0, i), colors.HexColor(CHIP[key])))
            style.append(("LEFTPADDING", (1, i), (1, i), 10))
        t = Table(data, colWidths=[52 * mm, CONTENT_W - 52 * mm], rowHeights=None)
        style += [("TOPPADDING", (0, 0), (0, -1), 9), ("BOTTOMPADDING", (0, 0), (0, -1), 9),
                  ("LINEBELOW", (0, 0), (-1, -2), 3, colors.white)]
        t.setStyle(TableStyle(style))
        self.append(t)
        self.append(Spacer(1, 8))


# --------------------------------------------------------------- dokument ---
class ManualDoc(BaseDocTemplate):
    def __init__(self, filename, **kw):
        super().__init__(filename, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                         topMargin=22 * mm, bottomMargin=24 * mm, title=DOC_TITLE,
                         author="XTB Trend Watch", subject=f"Instrukcja użytkownika, wersja {VERSION}", **kw)
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="main",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[frame]),
            PageTemplate(id="body", frames=[frame], onPage=self._footer),
        ])

    @staticmethod
    def _footer(canv, doc):
        canv.saveState()
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        canv.line(MARGIN_X, 17 * mm, PAGE_W - MARGIN_X, 17 * mm)
        canv.setFont("DejaVu", 8)
        canv.setFillColor(MUTED)
        canv.drawString(MARGIN_X, 12 * mm, DOC_TITLE)
        canv.drawRightString(PAGE_W - MARGIN_X, 12 * mm, f"Strona {doc.page}")
        canv.restoreState()

    def afterFlowable(self, flowable):
        # Klucze zakładek zależą od TEKSTU nagłówka (nie od licznika) - licznik rósłby z każdym
        # przebiegiem multiBuild i spis treści nigdy by się nie ustabilizował.
        if not isinstance(flowable, Paragraph):
            return
        name = flowable.style.name
        if name == "H1":
            text = flowable.getPlainText()
            key = f"h1:{text}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, 0, 0)
            self.notify("TOCEntry", (0, text, self.page, key))
        elif name == "H2":
            text = flowable.getPlainText()
            key = f"h2:{text}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, 1, 1)


def build(story: Story, out_path: str) -> None:
    check_glyph_coverage()

    cover = [
        Spacer(1, 78 * mm),
        Paragraph("◆", S["CoverMark"]),
        Paragraph("XTB Trend Watch", S["CoverTitle"]),
        Paragraph("Instrukcja użytkownika", S["CoverSub"]),
        Spacer(1, 16 * mm),
        Paragraph(f"Wersja dokumentu: {VERSION} • Data ostatniej aktualizacji: {DATE_LABEL}", S["CoverMeta"]),
        Spacer(1, 42 * mm),
        Paragraph("ZASTRZEŻENIE", S["CoverWarnHead"]),
        Paragraph(
            "XTB Trend Watch jest narzędziem analitycznym do własnego użytku, nie jest poradą "
            "inwestycyjną, rekomendacją w rozumieniu przepisów ani usługą doradztwa finansowego. "
            "Wszystkie decyzje inwestycyjne oraz związane z nimi ryzyko leżą wyłącznie po stronie "
            "użytkownika. Dane pochodzą z automatycznych, publicznie dostępnych źródeł (Yahoo Finance, "
            "Finnhub, oficjalne tabele NBP, kanały RSS) i lokalnego modelu językowego — mogą zawierać "
            "błędy, opóźnienia lub nieaktualne informacje. Zawsze weryfikuj kluczowe informacje "
            "samodzielnie przed podjęciem jakiejkolwiek decyzji finansowej.", S["CoverWarn"]),
        NextPageTemplate("body"),
        PageBreak(),
    ]

    toc = TableOfContents()
    toc.levelStyles = [S["TOC1"]]
    toc.dotsMinLevel = 0
    front = [Paragraph("Spis treści", S["TOCTitle"]), toc, PageBreak()]

    doc = ManualDoc(out_path)
    doc.multiBuild(cover + front + list(story))


# =====================================================================
# TREŚĆ
# =====================================================================
def part1(s: Story) -> None:

    # ------------------------------------------------------------ 1
    s.h1("1. Czym jest XTB Trend Watch")
    s.p("XTB Trend Watch to osobiste narzędzie analityczne, które codziennie (lub na żądanie) "
        "sprawdza spółki z Twojej watchlisty pod trzema kątami:")
    s.bullets([
        "<b>Analiza techniczna</b> — RSI, średnie kroczące (SMA), wstęgi Bollingera, ATR, odległość od "
        "maksimów 52-tygodniowych, potwierdzenie trendu z wyższego interwału (tygodniowego) oraz "
        "siła względna wobec benchmarku (np. S&amp;P 500 albo WIG20).",
        "<b>Sentyment newsów</b> — lokalny model językowy (LLM, działający offline przez Ollama) czyta "
        "najświeższe nagłówki dotyczące spółki i ocenia ich wydźwięk w skali od -1 do +1.",
        "<b>Fundamenty</b> — podstawowe wskaźniki finansowe (P/E, wzrost przychodów, marże, "
        "zadłużenie), konsensus cen docelowych analityków Wall Street oraz termin najbliższych "
        "wyników kwartalnych.",
    ])
    s.p("Wyniki tych trzech analiz są łączone w jeden wynik liczbowy (0–1) i przypisywana jest jedna z "
        "pięciu kategorii (patrz rozdział 6), które widzisz na kartach wyników. Poza analizą spółek "
        "narzędzie pomaga też pilnować własnych pozycji:")
    s.bullets([
        "<b>Portfel</b> — pozycje w wielu walutach, ocena „trzymaj / rozważ sprzedaż”, własny stop-loss "
        "i cel, ryzyko, korelacje między pozycjami, krzywa kapitału, import z raportu XTB.",
        "<b>Zamknięte transakcje</b> — historia wyników i orientacyjne podsumowanie podatkowe (PIT-38) "
        "liczone oficjalnym kursem NBP.",
        "<b>Alerty cenowe, brief dnia i asystent czatu</b> — wszystko oparte o lokalny model, bez wysyłania "
        "Twoich danych na zewnątrz.",
    ])
    s.callout("tip",
              "Narzędzie jest celowo konserwatywne — zawiera twarde blokady (np. nigdy nie sugeruje kupna "
              "tuż pod szczytem 52-tygodniowym ani zaraz po gwałtownym, jednorazowym spadku ceny) oraz "
              "mechanizmy chroniące przed fałszywymi sygnałami (potwierdzenie trendu z interwału "
              "tygodniowego). Oznacza to, że kategoria „WARTO OBSERWOWAĆ” pojawia się rzadziej niż mogłoby "
              "się wydawać — to zamierzone.")
    s.callout("important",
              "Narzędzie niczego nie kupuje ani nie sprzedaje i nie łączy się z Twoim kontem maklerskim. "
              "Generuje analizę, którą czytasz i na jej podstawie samodzielnie decydujesz, czy i jak złożyć "
              "zlecenie u swojego brokera.")

    # ------------------------------------------------------------ 2
    s.h1("2. Uruchamianie dashboardu")
    s.h2("2.1 Pierwsze uruchomienie (jednorazowo)")
    s.steps([
        f"W folderze projektu utwórz środowisko i zainstaluj zależności: {c('python3 -m venv venv')}, "
        f"aktywuj je, a następnie {c('pip install -r requirements.txt')}.",
        f"Skopiuj {c('config.example.yaml')} jako {c('config.yaml')} i uzupełnij watchlistę (tickery w "
        f"formacie Yahoo Finance, np. AAPL, ASML.AS, ALE.WA).",
        f"Opcjonalnie — lokalny model: zainstaluj Ollama, pobierz model ({c('ollama pull llama3.1:8b')}) "
        f"i uruchom {c('ollama serve')}. Bez modelu ustaw {c('llm.enabled: false')}: sentyment przełączy "
        f"się na prosty analizator słownikowy, a brief dnia, propozycje AI i czat po prostu się nie pojawią.",
        f"Opcjonalnie — newsy historyczne: darmowy klucz Finnhub wpisz w {c('news.finnhub_api_key')}. "
        f"Bez klucza ustaw {c('news.use_historical_news: false')}.",
    ])
    s.h2("2.2 Start serwera")
    s.p("Dashboard uruchamiasz z terminala, w folderze projektu:")
    s.code("python -m src.web_app --config config.yaml")
    s.p("Po uruchomieniu w terminalu pojawi się adres (domyślnie http://127.0.0.1:8000) — otwórz go w "
        f"przeglądarce. Jeżeli w konfiguracji {c('run_analysis_on_startup: true')}, pierwszy cykl analizy "
        "całej watchlisty rozpocznie się automatycznie zaraz po starcie serwera — może to potrwać od kilku "
        "do kilkunastu minut w zależności od liczby spółek i włączonych opcji. Pierwszy cykl jest "
        "najwolniejszy (zapełnia pamięć podręczną, patrz rozdział 19); kolejne są wyraźnie szybsze.")
    s.callout("warn",
              "Dashboard musi pozostać uruchomiony (okno terminala otwarte), żeby automatyczne cykle analizy "
              "i alerty cenowe działały w tle. Zamknięcie terminala zatrzymuje serwer.")
    s.h2("2.3 Tryb terminalowy (CLI)")
    s.p("Bez dashboardu możesz uruchomić jednorazową analizę samej watchlisty. Wynik pojawi się w terminalu "
        f"oraz jako pliki .md i .json w katalogu {c('reports/')}:")
    s.code("python -m src.main --config config.yaml")
    s.p("Tryb CLI da się uruchamiać codziennie automatycznie (cron w Linux/macOS albo Harmonogram zadań w "
        f"Windows z dołączonym plikiem {c('run_daily.bat')}). CLI nie obejmuje portfela, alertów ani czatu — "
        "to funkcje dashboardu.")

    # ------------------------------------------------------------ 3
    s.h1("3. Układ ekranu głównego")
    s.p("Dashboard składa się z trzech zakładek widocznych na górze ekranu:")
    s.bullets([
        "<b>Analiza</b> — główny widok: brief dnia, kontekst makro, watchlista, wyniki analizy, propozycje AI.",
        "<b>Portfel</b> — Twoje otwarte pozycje wraz z oceną, ryzykiem, korelacjami i krzywą kapitału "
        "(rozdziały 12–14).",
        "<b>Zamknięte transakcje</b> — sprzedane pozycje i podsumowanie podatkowe (rozdział 15).",
    ])
    s.p("W prawym górnym rogu znajdują się: wskaźnik połączenia na żywo (zielona pulsująca kropka = "
        "połączenie WebSocket aktywne), odliczanie do kolejnego automatycznego cyklu oraz przycisk "
        "„Odśwież teraz”, który wymusza natychmiastowy pełny cykl analizy (niezależnie od harmonogramu).")
    s.p("Na dole ekranu znajduje się zwijana szufladka „Log na żywo” (rozdział 16), a nad nią, w prawym "
        "dolnym rogu, okrągły przycisk czatu z asystentem (rozdział 18).")

    s.h2("3.1 Personalizacja układu")
    s.p("Dashboard bywa gęsty — każda zakładka ma sporo sekcji, z których nie wszystkie są dla Ciebie "
        "jednakowo ważne. Dwa niezależne mechanizmy pozwalają dopasować, co widać na ekranie; oba "
        "zapamiętywane są LOKALNIE w przeglądarce (osobno dla każdej przeglądarki/urządzenia) i działają "
        "bez połączenia z serwerem:")
    s.bullets([
        "<b>Panele boczne</b> (lewa kolumna zakładek Analiza i Portfel) — każdy panel ma w nagłówku uchwyt "
        "(ikona kropek, przeciągnij żeby zmienić kolejność), strzałki ▲/▼ (to samo bez przeciągania) i "
        "przycisk ▾ (zwiń/rozwiń). Kolejność i stan zwinięcia są pamiętane osobno dla panelu bocznego "
        "Analizy i osobno dla Portfela.",
        "<b>Sekcje głównej kolumny</b> (na wszystkich trzech zakładkach — np. „Ryzyko i ekspozycja”, "
        "„Krzywa kapitału”, „Historia transakcji”) — każdy nagłówek sekcji ma z prawej strony mały "
        "przycisk ▾, który zwija całą sekcję do samego nagłówka. Przydatne, gdy jakaś sekcja (np. "
        "macierz korelacji przy dużej watchliście) zajmuje dużo miejsca, a nie sprawdzasz jej za każdym "
        "razem.",
    ])
    s.callout("note",
              "Zwinięcie NIE wyłącza obliczeń ani danych w tle — to czysto wizualne ukrycie. Wykresy "
              "wewnątrz zwiniętej sekcji (np. krzywa kapitału) poprawnie doskalowują się z powrotem po "
              "jej rozwinięciu.")

    # ------------------------------------------------------------ 4
    s.h1("4. Panel boczny (zakładka Analiza)")
    s.h2("4.1 Kontekst makro")
    s.p("Pokazuje ogólny „nastrój rynku” niezależny od konkretnej spółki: indeks zmienności VIX, "
        "rentowność 10-letnich obligacji USA oraz wynikający z nich reżim ryzyka:")
    s.table(["Reżim", "Znaczenie"], [
        ["SPOKOJNY", "VIX poniżej progu podwyższonego ryzyka (domyślnie 20) — normalne warunki rynkowe."],
        ["PODWYŻSZONY", "VIX umiarkowanie podniesiony (domyślnie od 20) — próg sugestii kupna zostaje "
                        "nieznacznie podwyższony (o 0,05)."],
        ["WYSOKI", "VIX wyraźnie podniesiony (domyślnie od 30) — próg sugestii kupna podwyższony "
                   "wyraźniej (o 0,10); zachowaj szczególną ostrożność z wielkością pozycji."],
    ], [22, 78])
    s.p(f"Pod notatkami reżimu ryzyka panel pokazuje też <b>„Nadchodzące wydarzenia”</b> — najbliższe "
        f"zaplanowane wydarzenia makro w oknie 14 dni (domyślnie): posiedzenia FOMC i RPP/NBP z decyzją o "
        f"stopach procentowych oraz publikacje CPI (inflacja) USA. Lista dat pochodzi z "
        f"{c('config.yaml')} (sekcja {c('macro_calendar')}), gdzie jest publikowana z wyprzedzeniem przez "
        f"oficjalne instytucje (Fed, NBP, BLS) — narzędzie jej NIE scrapuje na żywo z zewnętrznych stron, "
        f"więc wymaga ręcznej aktualizacji raz na jakiś czas, gdy instytucje opublikują harmonogram na "
        f"kolejny rok (patrz rozdział 22).")
    s.h2("4.2 Koncentracja sektorowa")
    s.p("Jeżeli kilka spółek z Twojej watchlisty jednocześnie otrzyma kategorię „WARTO OBSERWOWAĆ” i "
        "należą do tego samego sektora (np. kilka spółek półprzewodnikowych), panel wyświetli "
        "ostrzeżenie. To sygnał, że nie są to niezależne okazje, tylko jeden skoncentrowany zakład "
        "sektorowy — warto to uwzględnić przy dywersyfikacji.")
    s.h2("4.3 Nadchodzące wyniki")
    s.p("Lista spółek (z watchlisty i propozycji AI), które w ciągu najbliższych 30 dni publikują wyniki "
        "kwartalne, posortowana od najbliższego terminu. Spółki, którym termin przypada w oknie "
        "ostrzegawczym (domyślnie 7 dni), są wyróżnione kolorem, a znacznik portfela oznacza spółki, "
        "które masz w otwartych pozycjach. Szczegóły w rozdziale 11.")
    s.h2("4.4 Skuteczność narzędzia")
    s.p("Ten panel porównuje przeszłe werdykty narzędzia z późniejszą, najnowszą znaną ceną tej samej "
        "spółki. Dla każdej kategorii pokazuje: liczbę ocenionych sygnałów, odsetek trafnych (dodatni "
        "zwrot) oraz średni zwrot procentowy.")
    s.callout("note",
              "To orientacyjny „rachunek sumienia”, nie pełny, rygorystyczny backtest. Panel wymaga "
              "upływu czasu (domyślnie min. 14 dni od danego werdyktu), by mieć co porównywać — przez "
              "pierwsze dni/tygodnie działania dashboardu będzie pokazywał komunikat „zbieram dane”. "
              "Można go zasilić historią symulowaną (rozdział 9).")
    s.h2("4.5 Watchlista — zarządzanie spółkami")
    s.p("Formularz pozwala dodać nową spółkę (ticker w formacie Yahoo Finance, np. AAPL, ASML.AS, ALE.WA) "
        "oraz opcjonalnie jej nazwę i symbol używany w XTB. Nowo dodana spółka pojawi się w wynikach po "
        "najbliższym cyklu analizy. Przycisk ✕ przy każdej pozycji usuwa spółkę z watchlisty (po "
        "potwierdzeniu). Spółki dodawane z zakładki Portfel trafiają na watchlistę automatycznie.")

    # ------------------------------------------------------------ 5
    s.h1("5. Karty wyników — jak czytać watchlistę")
    s.p("Każda spółka w sekcji „Stała watchlista” i „Propozycje AI” jest pokazana jako pozioma karta. "
        "Nad listą kart znajduje się nagłówek kolumn — kliknięcie w nazwę kolumny sortuje listę według "
        "niej (patrz rozdział 7). Karta zawiera, od lewej do prawej:")
    s.table(["Element", "Opis"], [
        ["Pieczęć (okrąg)", "Skrócona etykieta kategorii z kolorem: zielony = kupno, żółty = neutralnie, "
                            "szary = czekaj, czerwony = unikaj."],
        ["Spółka", "Ticker, pełna nazwa i symbol używany w XTB. Obok nazwy mogą się pojawić: znacznik wieku "
                   "danych (ikona zegara i np. „12 min temu”; kolor bursztynowy, gdy dane są starsze niż 2 "
                   "godziny) oraz bursztynowa plakietka „wyniki za N dni” (rozdział 11). Dla propozycji AI "
                   "dodatkowo krótkie uzasadnienie, dlaczego model ją zaproponował."],
        ["Cena docelowa", "Średnia cena docelowa analityków Wall Street (z Yahoo Finance) w walucie "
                          "notowania i różnica procentowa względem obecnej ceny. „—” oznacza brak danych "
                          "fundamentalnych."],
        ["Sygnał tech.", "Etykieta z czystej analizy technicznej: GOOD_ENTRY, NEUTRAL, WEAK_TREND, AT_TOP, "
                         "SHARP_DECLINE (patrz rozdział 6)."],
        ["Wynik / trend", "Łączny wynik 0–1 (technika + sentyment + fundamenty) oraz kierunek "
                          "12-miesięcznego trendu sentymentu newsów, jeśli dostępny."],
        ["Wykres", "Miniwykres ceny z ostatniego miesiąca (zielony, gdy cena jest wyżej niż na początku "
                   "okresu, czerwony w przeciwnym razie). To tylko orientacyjny podgląd — pełny wykres "
                   "otworzysz klikając kartę."],
        ["Kategoria", "Finalny werdykt narzędzia — patrz rozdział 6."],
    ], [22, 78])
    s.p("Kliknięcie w dowolną kartę otwiera pełny widok szczegółów tej spółki (rozdział 8).")
    s.p("Przycisk <b>„Heatmapa”</b> (ikona płomienia) nad listą przełącza widok „Stałej watchlisty” z kart na siatkę "
        "kolorowanych kafelków (jak np. na Finviz) — każda kafelka to ticker i procentowa zmiana ceny w "
        "OSTATNIEJ sesji, kolor od czerwonego (spadek) przez neutralny do zielonego (wzrost), nasycenie "
        "rośnie do +/-5%. Kliknięcie kafelki otwiera ten sam pełny widok szczegółów co karta. Przydatne do "
        "szybkiego przeglądu całej watchlisty bez przewijania — wybór widoku jest zapamiętywany lokalnie "
        "w przeglądarce.")

    # ------------------------------------------------------------ 6
    s.h1("6. Kategorie, sygnały i składniki wyniku")
    s.h2("6.1 Kategorie łączne (widoczne na pieczęci karty)")
    s.chips([
        ("buy", "WARTO OBSERWOWAĆ",
         "Wynik łączny powyżej progu ORAZ sygnał techniczny GOOD_ENTRY, potwierdzony z interwału "
         "tygodniowego. Najbardziej obiecująca kategoria — nadal wymaga własnej weryfikacji."),
        ("watch", "NEUTRALNIE POZYTYWNIE",
         "Wynik łączny powyżej progu, ale bez jednoznacznego sygnału technicznego wejścia (np. dobry "
         "sentyment przy braku potwierdzonego trendu wzrostowego)."),
        ("wait", "BRAK SYGNAŁU / POCZEKAJ",
         "Wynik łączny poniżej progu — najczęstsza kategoria. Brak wystarczających podstaw do działania "
         "w żadną stronę."),
        ("avoid", "UNIKAJ — blisko szczytu",
         "Twarda blokada: cena jest bardzo blisko maksimum 52-tygodniowego (domyślnie w odległości do 5%) lub "
         "RSI wskazuje wykupienie. Nigdy nie trafia do sugestii kupna, niezależnie od sentymentu."),
        ("avoid", "UNIKAJ — gwałtowny spadek",
         "Twarda blokada: cena spadła gwałtownie (domyślnie co najmniej 15% w 5 sesjach) — to wygląda na "
         "reakcję na konkretny, zły news, a nie zdrową korektę. Narzędzie celowo nie nagradza tu "
         "„taniości” bez ręcznej weryfikacji przyczyny."),
    ])
    s.h2("6.2 Sygnały techniczne (surowa analiza, przed uwzględnieniem sentymentu)")
    s.table(["Sygnał", "Znaczenie"], [
        ["GOOD_ENTRY", "Trend wzrostowy (SMA50 &gt; SMA200), zdrowe cofnięcie od lokalnego szczytu, "
                       "potwierdzone na interwale tygodniowym."],
        ["NEUTRAL", "Brak jednoznacznej przewagi w żadną stronę."],
        ["WEAK_TREND", "Brak potwierdzonego trendu wzrostowego i niski wynik techniczny."],
        ["AT_TOP", "Cena bardzo blisko maksimum 52-tygodniowego lub RSI wykupiony."],
        ["SHARP_DECLINE", "Wykryto gwałtowny, jednorazowy spadek ceny w krótkim oknie czasowym."],
    ], [22, 78], mono_first_col=True)
    s.callout("note",
              "Sygnał dzienny GOOD_ENTRY bez potwierdzenia trendem tygodniowym zostaje automatycznie "
              "obniżony do NEUTRAL — to ochrona przed tzw. „pułapką byka” (odbicie na wykresie dziennym "
              "w ramach szerszego trendu spadkowego).")
    s.h2("6.3 Z czego składa się wynik łączny")
    s.table(["Składnik", "Wpływ na wynik"], [
        ["Analiza techniczna", "Główny składnik (domyślna waga 55%)."],
        ["Sentyment newsów", "Waga domyślnie 30%. Bez modelu LLM używany jest prosty analizator słownikowy."],
        ["Fundamenty", "Waga domyślnie 15%. Jeśli dane fundamentalne są niedostępne, ich waga jest "
                       "przenoszona proporcjonalnie na pozostałe składniki."],
        ["Trend sentymentu (12 mies.)", "Niewielka korekta ±0,05: poprawiający się sentyment — premia, "
                                        "pogarszający się — kara."],
        ["Siła względna vs benchmark", "Niewielka korekta ±0,05, gdy spółka bije benchmark (lub przegrywa z "
                                       "nim) o co najmniej 10 pkt proc. w ostatnich 60 sesjach. Benchmark "
                                       "zależy od waluty: USD — SPY, PLN — WIG20, EUR — STOXX 50. Gdy główny "
                                       "benchmark jest chwilowo niedostępny w Yahoo Finance (typowe dla WIG20), "
                                       "narzędzie automatycznie sięga po zastępcze źródło (np. Stooq) — patrz "
                                       "rozdział 21."],
        ["Reżim makro (VIX)", "Nie zmienia wyniku, tylko PODNOSI PRÓG sugestii kupna (rozdział 4.1)."],
        ["Cena docelowa analityków", "Wyłącznie informacyjnie — celowo NIE wpływa na wynik, żeby nie "
                                     "liczyć dwa razy tego samego sentymentu rynkowego."],
        ["Wyniki kwartalne", "Wyłącznie ostrzeżenie — nie wpływa na wynik ani kategorię (rozdział 11)."],
    ], [26, 74])
    s.p("<b>Wyjątek dla ETF-ów.</b> Bliskość maksimum 52-tygodniowego jest normalna dla zdywersyfikowanego "
        "funduszu indeksowego w długim trendzie wzrostowym, więc dla ETF-ów sama ta bliskość nie powoduje "
        "blokady „UNIKAJ — blisko szczytu”. Wykupienie wg RSI nadal jest traktowane jako sygnał "
        "ostrożności także dla ETF-ów.")
    s.p(f"<b>Próg RSI wyprzedania</b> wynosi domyślnie 30 (wcześniej 35) — zmianę wprowadzono po walidacji "
        f"na historycznych danych (rozdział 9). Możesz go dostosować w {c('technical.rsi_oversold')}.")

    # ------------------------------------------------------------ 7
    s.h1("7. Sortowanie i porównywanie spółek")
    s.p("Nagłówki kolumn nad każdą listą (watchlista, propozycje AI, portfel, zamknięte transakcje) są "
        "klikalne. Pierwsze kliknięcie sortuje rosnąco (strzałka ▲), drugie kliknięcie tej samej kolumny "
        "odwraca kierunek (▼). Aktywna kolumna sortowania jest podświetlona na złoto. Kolumna „Wykres” "
        "z miniwykresem nie służy do sortowania.")
    s.p("Przykładowe zastosowania: sortowanie po „Wynik / trend” malejąco, by zobaczyć najsilniejsze "
        "sygnały na górze; sortowanie po „Cena docelowa” malejąco, by znaleźć spółki z największym "
        "potencjałem wg analityków Wall Street niezależnie od bieżącej kategorii narzędzia.")

    # ------------------------------------------------------------ 8
    s.h1("8. Szczegóły spółki — wykres i pełna analiza")
    s.p("Kliknięcie w kartę spółki otwiera okno z wykresem świecowym oraz pełnym panelem analizy. Okno "
        "pokazuje ostatnio znane dane z ostatniego cyklu — <b>nic nie jest odświeżane automatycznie</b> "
        "przy samym otwarciu, dzięki czemu możesz szybko przeglądać kilka spółek pod rząd bez generowania "
        "ruchu sieciowego. Świeże dane pobierzesz przyciskiem „Odśwież tę spółkę” na górze panelu "
        "(patrz rozdział 19). Okno zamkniesz krzyżykiem, klawiszem Esc albo kliknięciem w tło.")
    s.h2("8.1 Wykres")
    s.bullets([
        "Złota linia — SMA 50 (średnia z 50 sesji, trend krótkoterminowy).",
        "Fioletowa linia — SMA 200 (średnia z 200 sesji, trend długoterminowy). Pojawia się dopiero, gdy "
        "dostępna jest wystarczająca historia cen.",
        "Znaczniki na świecach — historia werdyktów narzędzia dla tej spółki: zielony trójkąt „KUP” "
        "(WARTO OBSERWOWAĆ), czerwony trójkąt „SZCZYT” lub „SPADEK” (odpowiednie kategorie UNIKAJ), "
        "pomarańczowe kółko „N” (NEUTRALNIE). Kategoria „BRAK SYGNAŁU” celowo nie ma znacznika — "
        "zaśmiecałaby wykres.",
    ])
    s.p("<b>Jeśli spółka jest w Twoim portfelu</b>, wykres dorysowuje dodatkowo poziome linie referencyjne: "
        "czerwoną przerywaną — aktualny stop-loss (własny albo z ATR, opisany w tytule linii), cyjanową "
        "przerywaną — Twój własny cel cenowy (jeśli go ustawiłeś), oraz szarą kropkowaną — średnią cenę "
        "zakupu. Pod wykresem pojawia się wtedy druga linijka legendy z dokładnymi wartościami i skrótem "
        "powodów bieżącej rekomendacji pozycji. Dzięki temu widać od razu, DLACZEGO poleciał dany alert "
        "cenowy (rozdział 16) — świeca leżąca poniżej czerwonej linii to właśnie przebity stop-loss.")
    s.callout("tip",
              "Każdy z tych trzech poziomów (stop, cel, świece wzrostowe/„KUP”) ma celowo INNY kolor niż "
              "reszta wykresu — stop-loss dzielił wcześniej barwę z SMA 200, a cel z wykupionymi świecami "
              "i znacznikiem „KUP”, przez co linie zlewały się ze sobą.")
    s.h2("8.2 Panel analizy (pod wykresem)")
    s.table(["Sekcja", "Zawartość"], [
        ["Przycisk odświeżania", "„Odśwież tę spółkę (ceny, sentyment, newsy)” — pobiera świeże dane tylko dla "
                                 "tej jednej spółki. Obok wyświetla się status ostatniego odświeżenia."],
        ["Podsumowanie", "Kategoria, wynik łączny, próg sugestii oraz informacja o ewentualnej twardej "
                         "blokadzie."],
        ["Baner wyników", "Bursztynowy baner, jeśli wyniki kwartalne spółki są blisko (rozdział 11)."],
        ["Analiza techniczna", "Pełna lista powodów (RSI, trend, cofnięcie, wolumen, siła względna vs "
                               "benchmark) oraz kluczowe metryki."],
        ["Sentyment newsów", "Ocena bieżącego sentymentu przez lokalny LLM wraz z uzasadnieniem."],
        ["Aktualne nagłówki", "Lista nagłówków, na podstawie których LLM ocenił sentyment — z linkami do "
                              "źródeł."],
        ["Trend sentymentu (12 mies.)", "Jeśli włączone newsy historyczne (Finnhub) — czy narracja wokół "
                                        "spółki poprawia się, pogarsza, czy jest stabilna."],
        ["Fundamenty", "P/E, wzrost przychodów, marża netto, zadłużenie, cena docelowa i potencjał wg "
                       "analityków, data następnych wyników — z flagami interpretacyjnymi."],
        ["Kalkulator wielkości pozycji", "Patrz rozdział 8.3."],
        ["Uzasadnienie AI (dla propozycji)", "Dlaczego lokalny model zaproponował tę spółkę spoza "
                                             "watchlisty."],
    ], [26, 74])
    s.h2("8.3 Kalkulator wielkości pozycji")
    s.p("Na podstawie ATR (Average True Range — miary typowej dziennej zmienności) narzędzie sugeruje "
        "poziom stop-loss jako: cena bieżąca − 2 × ATR(14). Wpisując swój kapitał i procent ryzyka, jaki "
        "akceptujesz na pojedynczą transakcję, kalkulator wylicza:")
    s.bullets([
        "kwotę, którą realnie ryzykujesz przy trafieniu sugerowanego stop-lossu,",
        "sugerowaną liczbę akcji do kupienia,",
        "łączną wartość takiej pozycji.",
    ])
    s.callout("tip",
              "To punkt wyjścia do własnego zarządzania ryzykiem, nie gotowa rekomendacja. Kapitał wpisuj "
              "w walucie notowania spółki. Wartości kapitału i procentu ryzyka zapamiętywane są lokalnie w "
              "przeglądarce między sesjami.")


def part2(s: Story) -> None:
    # ------------------------------------------------------------ 9
    s.h1("9. Skuteczność narzędzia i backtest")
    s.p("Panel „Skuteczność narzędzia” (rozdział 4.4) to szybki, stale aktualizowany wgląd w to, czy "
        "sygnały danej kategorii historycznie się sprawdzały. Dokładniejsze sprawdzenie logiki technicznej "
        "na dłuższej próbie umożliwiają polecenia terminalowe, uruchamiane osobno od dashboardu.")
    s.h2("9.1 Backtest")
    s.table(["Polecenie", "Do czego służy"], [
        ["python -m src.backtest --ticker MSFT --years 5",
         "Pojedynczy backtest: co by było, gdyby przez N lat wchodzić w pozycję przy każdym sygnale "
         "GOOD_ENTRY i wychodzić po ustalonym czasie (--hold-days, domyślnie 20) lub przy sygnale AT_TOP."],
        ["python -m src.backtest --grid-search --years 5",
         "Przeszukuje kombinacje parametrów (czas trzymania, RSI wyprzedania, wymagane cofnięcie od szczytu) "
         "na wielu spółkach naraz i wyświetla ranking według średniego zwrotu."],
        ["python -m src.backtest --walk-forward --years 5 --test-years 2",
         "Uczciwsza wersja grid search: najlepsze kombinacje z okresu treningowego są sprawdzane na "
         "osobnym, nieużytym do optymalizacji okresie testowym, obok punktu odniesienia z Twojego "
         "config.yaml."],
    ], [38, 62], mono_first_col=True)
    s.h2("9.2 Zasilenie panelu skuteczności historią")
    s.p("Zamiast czekać tygodniami na naturalne zbieranie werdyktów, możesz je zasymulować na realnych "
        "cenach z przeszłości:")
    s.code("python -m src.backfill_history --years 2 --step-days 5")
    s.p(f"Opcje: {c('--tickers MSFT,AAPL')} (wybrane spółki), {c('--clear-first')} (usuń wcześniejsze wpisy "
        "backfillu, unika duplikatów). Symulacja obejmuje wyłącznie analizę TECHNICZNĄ — sentyment newsów, "
        "fundamenty i kontekst makro są pomijane, bo nie ma ich za darmo w wersji historycznej. Po backfillu "
        "panel skuteczności odzwierciedla więc głównie logikę techniczną, a nie pełny pipeline z dashboardu.")
    s.h2("9.3 Jak czytać wyniki")
    s.bullets([
        "Backtest testuje wyłącznie logikę techniczną, bez sentymentu i fundamentów, i nie uwzględnia "
        "prowizji, spreadu, poślizgu cenowego ani podatku.",
        "Optymalizacja parametrów na tych samych danych (zwykły grid search) grozi przeuczeniem — dlatego "
        "istnieje tryb walk-forward. Szukaj kombinacji, które są dobre także w okresie testowym.",
        "Wysokie wyniki z ostatnich lat mogą w dużej mierze odzwierciedlać wyjątkową hossę w wąskim "
        "sektorze (np. AI/tech), a nie trwałą przewagę strategii. Spółki z jednego sektora są mocno "
        "skorelowane, więc 15 spółek to bliżej jednego dużego zakładu niż 15 niezależnych testów.",
    ])
    s.callout("note",
              "Obecna domyślna wartość RSI wyprzedania (30 zamiast 35) wynika z walidacji walk-forward: "
              "przewaga utrzymała się w obu oknach czasowych. To nie gwarantuje podobnych wyników w innym "
              "reżimie rynkowym (bessa, trend boczny).")

    # ------------------------------------------------------------ 10
    s.h1("10. Propozycje AI (Discovery)")
    s.p("Jeśli funkcja odkrywania jest włączona w konfiguracji, lokalny model językowy analizuje bieżące "
        "nagłówki makroekonomiczne i proponuje do kilku spółek spoza Twojej stałej watchlisty, które mogą "
        "być warte obserwacji. Każdy kandydat jest następnie przepuszczany przez dokładnie tę samą pełną "
        "analizę (techniczną, sentymentową, fundamentalną), co spółki ze stałej listy.")
    s.callout("warn",
              "Model językowy może „zhalucynować” nieistniejący lub nieaktualny ticker — każda propozycja "
              "jest więc traktowana jako niezweryfikowana, dopóki nie uda się realnie pobrać dla niej "
              "danych rynkowych. Ta sama spółka nie zostanie zaproponowana ponownie przez określoną liczbę "
              "dni (domyślnie 14) — to zapobiega powtarzaniu tych samych propozycji w kółko.")
    s.p("Propozycje AI zawsze wymagają własnej weryfikacji przed jakąkolwiek decyzją — sekcja jest wyraźnie "
        "oznaczona etykietą „wymaga weryfikacji”.")

    # ------------------------------------------------------------ 11
    s.h1("11. Wyniki kwartalne i ostrzeżenia o zmienności")
    s.p("Publikacja wyników kwartalnych często powoduje gwałtowną zmianę ceny w obie strony — także wtedy, "
        "gdy liczby są dobre. Stop-loss oparty o ATR nie chroni przed luką cenową po takiej publikacji, "
        "dlatego dashboard ostrzega, gdy termin wyników się zbliża.")
    s.h2("11.1 Gdzie widać ostrzeżenie")
    s.bullets([
        "<b>Karty watchlisty i portfela</b> — bursztynowa plakietka „wyniki za N dni” (z ikoną kalendarza), "
        "gdy termin mieści się w oknie ostrzegawczym (domyślnie 7 dni).",
        "<b>Panel szczegółów spółki</b> — bursztynowy baner z datą oraz komórka „Następne wyniki” w "
        "sekcji Fundamenty.",
        "<b>Panel boczny „Nadchodzące wyniki”</b> — lista terminów na 30 dni do przodu; znacznik portfela "
        "oznacza spółki, które posiadasz.",
        "<b>Portfel</b> — dodatkowa uwaga w powodach rekomendacji danej pozycji.",
        "<b>Brief dnia i czat</b> — wzmianka o wynikach w najbliższych dniach (brief: do 10 dni).",
    ])
    s.h2("11.2 Czego to ostrzeżenie NIE robi")
    s.p("Ostrzeżenie jest wyłącznie informacją. Nie zmienia wyniku łącznego, kategorii ani rekomendacji "
        "portfelowej (TRZYMAJ / ROZWAŻ SPRZEDAŻ...) — to nie jest sygnał „kupuj” ani „sprzedaj”, tylko "
        "przypomnienie o podwyższonym ryzyku zmienności. Czy i jak na nie zareagować (wielkość pozycji, "
        "stop-loss, przeczekanie), decydujesz samodzielnie.")
    s.h2("11.3 Skąd pochodzi data")
    s.p("Termin pobierany jest z Yahoo Finance (kalendarz spółki, a w razie braku — pole terminu wyników "
        "w danych podstawowych). Yahoo bywa niedokładne, a data często jest szacunkowa i może się "
        "przesunąć — potwierdź ją w kalendarzu relacji inwestorskich spółki. Dla ETF-ów i funduszy daty "
        "nie ma. Próg ostrzegania zmienisz parametrem "
        f"{c('fundamentals.earnings_warning_days')} (domyślnie 7).")

    # ------------------------------------------------------------ 12
    s.h1("12. Zakładka Portfel")
    s.p("Zakładka „Portfel” pozwala zapisać posiadane pozycje i na bieżąco widzieć, jak wypadają w świetle "
        "aktualnej analizy narzędzia — czy sugerowane jest trzymanie, rozważenie sprzedaży, czy realizacja "
        "zysku.")
    s.h2("12.1 Dodawanie pozycji")
    s.p("Formularz po lewej stronie wymaga: tickera, liczby akcji, ceny zakupu i daty zakupu. Opcjonalne są: "
        "<b>własny stop-loss</b>, <b>własny cel cenowy</b> i notatka. Po dodaniu spółka automatycznie trafia "
        "też na watchlistę (jeśli jeszcze jej tam nie było), żeby mogła być regularnie analizowana.")
    s.p("Własny stop-loss i cel wpisujesz w walucie notowania spółki. Jeśli ustawisz własny stop-loss, ma on "
        "pierwszeństwo przed sugestią z ATR: wpływa na rekomendację pozycji, na panel ryzyka portfela "
        "(rozdział 14) i na alerty cenowe (rozdział 16). Własny cel uruchamia ocenę „ROZWAŻ REALIZACJĘ "
        "ZYSKU” i osobny alert po jego osiągnięciu. Oba pola możesz w każdej chwili zmienić lub wyczyścić "
        "przez edycję pozycji.")
    s.p("Formularz ma też opcjonalne pole <b>„Własny kurs wymiany”</b> — przydatne, jeśli kupujesz akcje "
        "zagraniczne przez brokera, który dolicza własną marżę do kursu wymiany (np. XTB, ok. 0,5%), przez co "
        "realny koszt zakupu w Twojej walucie bazowej jest inny niż wynikałoby z bieżącego kursu rynkowego. "
        "Jeśli podasz tu rzeczywisty kurs zastosowany przez brokera, dashboard użyje go zamiast bieżącego "
        "kursu rynkowego do przeliczenia kosztu TEJ pozycji na walutę bazową — w podsumowaniu łącznym "
        "portfela (rozdział 13.1) i w krzywej kapitału TWR (rozdział 14.4.2). Puste pole nie zmienia "
        "niczego — dashboard po prostu użyje bieżącego kursu, tak jak dotąd.")
    s.callout("note",
              "To pole dotyczy tylko przeliczenia na walutę bazową do celów poglądowych. Podsumowanie "
              "podatkowe (rozdział 15.2) zawsze liczy oficjalnym kursem NBP z dnia transakcji, niezależnie od "
              "tego pola — tak wymaga art. 11a ustawy o PIT.")
    s.p("Checkbox <b>„Konto IKE”</b> oznacza pozycję jako kupioną na polskim Indywidualnym Koncie Emerytalnym "
        "— wpływa WYŁĄCZNIE na to, jak pozycja jest liczona w podsumowaniu podatkowym (rozdział 15.2, gdzie "
        "jest pełne wyjaśnienie), nigdzie indziej. Przy imporcie z XTB (rozdział 12.4) ten checkbox jest "
        "ustawiany automatycznie na podstawie raportu.")
    s.h2("12.2 Grupowanie pozycji")
    s.p("Kilka transakcji na tej samej spółce (np. dokupowanie w różnych momentach) jest automatycznie "
        "zwijane w jedną kartę zbiorczą — widoczna jest łączna liczba akcji, średnia cena zakupu, "
        "sumaryczny wynik oraz znaczniki wieku danych i zbliżających się wyników. Kliknięcie strzałki "
        "„rozwiń” pokazuje poszczególne transakcje z osobna, razem z horyzontem inwestycji "
        "(krótkoterminowa &lt; 1 mies., średnioterminowa &lt; 1 rok, długoterminowa &gt; 1 rok) i ustawionymi "
        "własnymi poziomami stop/cel.")
    s.h2("12.3 Przyciski przy pojedynczej transakcji")
    s.bullets([
        "<b>Sprzedaj</b> (ikona worka pieniędzy) — pytanie o cenę sprzedaży, a potem opcjonalnie o rzeczywisty "
        "kurs wymiany brokera przy tej sprzedaży (analogicznie do pola z rozdziału 12.1 przy dodawaniu — "
        "puste pole = użyj bieżącego kursu rynkowego); pozycja przenosi się do zakładki „Zamknięte "
        "transakcje”. Data sprzedaży ustawiana jest na dzień kliknięcia.",
        "<b>✎ Edytuj</b> — wypełnia formularz danymi tej transakcji; po zapisaniu zmian transakcja jest "
        "aktualizowana (nie tworzy nowej). Przycisk „Anuluj edycję” wraca do trybu dodawania.",
        "<b>Kopiuj</b> (ikona dwóch kartek) — wypełnia formularz danymi tej transakcji z dzisiejszą datą, "
        "przygotowując go do zapisania jako nowa pozycja — przydatne przy dopisywaniu kolejnego zakupu tej "
        "samej spółki.",
        "<b>✕ Usuń</b> — trwale usuwa pojedynczą transakcję (po potwierdzeniu).",
    ])
    s.p("Na karcie zbiorczej dostępny jest też przycisk „Analiza AI” (ikona lupy), który otwiera pełny "
        "widok szczegółów spółki (rozdział 8), żeby zobaczyć cały kontekst stojący za rekomendacją.")
    s.h2("12.4 Import z raportu XTB")
    s.p("Pod formularzem znajduje się sekcja „Import z raportu XTB”. Wybierz plik .xlsx wyeksportowany z XTB "
        "(arkusze „Open Positions” i „Closed Positions”) i kliknij „Importuj pozycje”.")
    s.bullets([
        "Importowane są pojedyncze, długie pozycje (BUY). Pozycje otwarte trafiają do portfela, zamknięte — "
        "od razu do zakładki „Zamknięte transakcje” z cenami i datami z raportu (to najdokładniejszy "
        "sposób na wprowadzenie historii pod rozliczenie podatkowe).",
        "Import jest bezpieczny do wielokrotnego uruchamiania: każda pozycja dostaje znacznik z numerem XTB "
        "w notatce, więc ponowny import tego samego lub nowszego pliku nie tworzy duplikatów, a pozycje "
        "wpisane ręcznie nie są ruszane. Jeśli pozycja była zaimportowana jako OTWARTA, a od tego czasu "
        "sprzedałeś ją u brokera, ponowny import nowszego raportu automatycznie zamknie ją w dashboardzie "
        "(cena, data i kurs sprzedaży z raportu) — nie trzeba jej zamykać ręcznie.",
        "Symbole XTB są mapowane na Yahoo Finance według sufiksu giełdy, m.in.: .US → bez sufiksu, "
        ".PL → .WA, .DE → .DE, .UK → .L, .FR → .PA, .NL → .AS, .ES → .MC, .IT → .MI. Waluta jest wstępnie "
        "zgadywana z sufiksu, a potem korygowana danymi z Yahoo przy pierwszym cyklu analizy.",
        "Przy nierozpoznanym sufiksie narzędzie zgłasza ostrzeżenie (także w logu na żywo) — zweryfikuj taki "
        "ticker i w razie potrzeby popraw go przyciskiem ✎.",
        "Rzeczywisty kurs wymiany zastosowany przez brokera (razem z jego marżą) jest wyliczany automatycznie "
        "z wartości w raporcie XTB i zapisywany przy każdej zaimportowanej pozycji — nie trzeba wpisywać go "
        "ręcznie (pole z rozdziału 12.1 służy do pozycji dodawanych ręcznie). Jeśli plik importujesz ponownie "
        "(np. nowszy eksport) i część pozycji była już wcześniej dodana ręcznie bez tego kursu, dashboard "
        "dopisuje im go retrospektywnie, nie ruszając żadnych innych danych — po imporcie licznik takich "
        "uzupełnień pokazuje się w komunikacie podsumowującym.",
        "Konto IKE (rozdział 12.1) jest rozpoznawane automatycznie z kolumny „Product” w raporcie XTB — "
        "jeśli Twoje konto to subkonto IKE, wszystkie zaimportowane pozycje dostają tę flagę bez potrzeby "
        "ręcznego zaznaczania. Ponowny import starszego pliku (sprzed wprowadzenia tej funkcji) dopisuje "
        "flagę retrospektywnie tym samym mechanizmem co kurs wymiany wyżej — tylko z 'standard' na 'IKE', "
        "nigdy w drugą stronę.",
    ])
    s.h2("12.5 Rekomendacja „trzymaj / sprzedaj”")
    s.p("Każda karta (i grupa) otrzymuje jedną z etykiet:")
    s.table(["Rekomendacja", "Warunek"], [
        ["TRZYMAJ", "Brak sygnału zmiany — domyślny stan."],
        ["ROZWAŻ SPRZEDAŻ", "Bieżąca cena spadła poniżej stop-lossu: Twojego własnego (jeśli ustawiony), w "
                            "przeciwnym razie sugerowanego (2×ATR)."],
        ["ROZWAŻ REALIZACJĘ ZYSKU", "Narzędzie wskazuje „blisko szczytu trendu”, a pozycja jest na plusie — "
                                    "albo cena osiągnęła Twój własny cel."],
        ["SPRAWDŹ PRZYCZYNĘ — NIE DOKUPUJ", "Wykryto świeży, gwałtowny spadek ceny — wymaga ręcznej "
                                            "weryfikacji przyczyny przed jakąkolwiek decyzją."],
        ["BRAK DANYCH", "Spółka nie została jeszcze przeanalizowana (np. dopiero co dodana)."],
    ], [30, 70])
    s.p("Pod etykietą wypisane są powody rekomendacji, w tym uwaga o zbliżających się wynikach kwartalnych "
        "(rozdział 11), jeśli dotyczy. Najechanie na kolorową pieczątkę rekomendacji pokazuje te same powody "
        "w podpowiedzi (tooltip), bez rozwijania karty.")
    s.p("Każda karta pokazuje też <b>aktywny stop-loss i cel</b> — nie tylko wtedy, gdy ustawiłeś własny, ale "
        "zawsze: własny, jeśli go podałeś, w przeciwnym razie sugerowany z ATR (z etykietą, które to źródło). "
        "To ta sama wartość, której pilnują alerty cenowe (rozdział 16) i która jest rysowana jako linia na "
        "wykresie (rozdział 8.1) — dzięki temu nie trzeba zgadywać, przy jakiej cenie dokładnie coś się "
        "wydarzy.")
    s.callout("important",
              "Rekomendacje portfelowe to proste, przejrzyste reguły łączące cenę zakupu, bieżącą cenę i "
              "sygnał techniczny — nie uwzględniają Twojej sytuacji podatkowej, kosztów transakcyjnych ani "
              "indywidualnej tolerancji ryzyka. To nie jest porada inwestycyjna ani podatkowa.")

    s.h2("12.6 Dywidendy")
    s.p("Na dole zakładki Portfel znajduje się sekcja „Dywidendy” — podsumowanie otrzymanego przychodu "
        "(brutto, podatek u źródła, netto) w walucie bazowej, rozbite wg roku, oraz pełna tabela "
        "pojedynczych wypłat z możliwością usunięcia dowolnej pozycji (✕).")
    s.bullets([
        "Import z raportu XTB (rozdział 12.4) wczytuje wypłacone dywidendy automatycznie z arkusza „Cash "
        "Operations” — nie trzeba wpisywać ich ręcznie. Kwota brutto i ewentualny podatek u źródła "
        "potrącony przez brokera parowane są po numerze pozycji, a nie po czasie operacji (dokładniejsze "
        "przy kilku dywidendach tego samego dnia). Import jest idempotentny jak reszta raportu — ponowne "
        "wczytanie tego samego lub nowszego pliku nie tworzy duplikatów.",
        "Formularz „Dodaj dywidendę ręcznie” w panelu bocznym pozwala dopisać wypłatę spoza raportu XTB "
        "(np. inny broker) — ticker, kwota brutto, waluta, data, opcjonalnie podatek u źródła i notatka.",
    ])
    s.callout("note",
              "To podsumowanie przepływu gotówki, NIE rozliczenie podatkowe. Polski podatek od dywidend "
              "zagranicznych to różnica między 19% a podatkiem u źródła już potrąconym za granicą (jeśli "
              "stawka źródłowa jest niższa) — narzędzie tej ewentualnej dopłaty nie wylicza, podobnie jak "
              "nie uwzględnia specyfiki kont zwolnionych z podatku (np. IKE). Kwoty w walucie bazowej "
              "przeliczane są, tak jak w podsumowaniu podatkowym (rozdział 15.2), oficjalnym kursem NBP z "
              "dnia poprzedzającego wypłatę.")

    # ------------------------------------------------------------ 13
    s.h1("13. Waluty i podsumowanie łączne portfela")
    s.p("Każda spółka ma wykrytą walutę notowania (np. USD dla spółek amerykańskich, PLN dla polskich jak "
        "XTB.WA czy SNT.WA). Waluta jest widoczna wszędzie: na kartach wyników, w panelu szczegółów, w "
        "kalkulatorze wielkości pozycji i przy każdej pozycji w portfelu. Wpisując pozycję do portfela, "
        "cenę zakupu podajesz w walucie notowania danej spółki — dashboard sam ją wykrywa i dopisuje przy "
        "pierwszym pełnym cyklu analizy po dodaniu pozycji.")
    s.h2("13.1 Podsumowanie per waluta i podsumowanie łączne")
    s.bullets([
        "<b>Podsumowanie łączne</b> (złota karta na samej górze zakładki Portfel) — wszystkie pozycje, "
        "niezależnie od natywnej waluty, przeliczone na jedną, wspólną walutę bazową (domyślnie PLN, "
        f"parametr {c('portfolio.base_currency')}) i zsumowane w jedną wartość, koszt, wynik oraz ryzyko "
        "przy stop-lossach.",
        "<b>Podsumowanie per waluta</b> (poniżej) — te same pozycje, ale rozbite osobno dla każdej waluty "
        "bez przeliczania — przydatne, żeby zobaczyć wynik dokładnie w takiej walucie, w jakiej realnie "
        "zainwestowałeś.",
    ])
    s.callout("note",
              "Bieżąca wartość pozycji jest zawsze przeliczana BIEŻĄCYM kursem rynkowym (yfinance) w momencie "
              "każdego cyklu analizy. Koszt zakupu (i tym samym wynik) natomiast używa rzeczywistego kursu "
              "brokera, jeśli go znasz i podałeś (import z XTB — automatycznie, rozdział 12.4; pozycja dodana "
              "ręcznie — opcjonalne pole z rozdziału 12.1) — w przeciwnym razie też bieżącego kursu "
              "rynkowego, tak jak dotąd. To wystarczające do orientacyjnego podglądu portfela; do rozliczeń "
              "podatkowych używany jest zawsze i wyłącznie oficjalny kurs NBP (rozdział 15). Jeśli kursu "
              "jakiejś waluty chwilowo nie da się pobrać, karta łączna pokazuje adnotację, że ta waluta "
              "została pominięta.")

    # ------------------------------------------------------------ 14
    s.h1("14. Ryzyko, korelacje, benchmark i krzywa kapitału")
    s.h2("14.1 Panel „Ryzyko i ekspozycja”")
    s.p("Widoczny w zakładce Portfel, pod podsumowaniami, pokazuje:")
    s.bullets([
        "<b>Ryzyko przy stop-lossach</b> — ile realnie stracisz (kwotowo i procentowo wartości portfela), "
        "jeśli KAŻDA otwarta pozycja spadnie dokładnie do swojego stop-lossu (własnego, a w razie jego braku "
        "sugerowanego 2×ATR). To „najgorszy rozsądny scenariusz” przy obecnym ustawieniu stopów, nie "
        "prognoza. Pozycje bez policzonego stop-lossu są pominięte, o czym panel informuje.",
        "<b>Ekspozycja sektorowa portfela</b> — w przeciwieństwie do koncentracji sektorowej z panelu "
        "bocznego (która dotyczy watchlisty, czyli tego, co OBSERWUJESZ), pokazuje, ile % Twojego "
        "REALNEGO kapitału siedzi w poszczególnych sektorach, ważone wartością rynkową pozycji "
        "przeliczoną na walutę bazową.",
    ])
    s.h2("14.2 Panel „Korelacja i zmienność”")
    s.p("Odpowiada na pytanie, czy kilka posiadanych spółek to naprawdę kilka niezależnych zakładów, czy "
        "w praktyce jeden (np. wszystkie z sektora AI/tech). Statystyki liczone są z ostatniego roku "
        "notowań dla obecnych wag pozycji (wartość rynkowa przeliczona na walutę bazową). Liczenie chwilę "
        "trwa, bo dashboard pobiera historię cen; po każdej zmianie w portfelu panel odświeża się sam.")
    s.table(["Miara", "Jak ją czytać"], [
        ["Zmienność roczna", "Odchylenie standardowe dziennych zmian wartości portfela przeliczone na rok. "
                             "Im wyżej, tym większe wahania — przy 40% i więcej panel wyświetla ostrzeżenie."],
        ["Sharpe", "(Roczny zwrot − stopa wolna od ryzyka) / zmienność. Powyżej 1 to dobry wynik "
                   "skorygowany o ryzyko (zielony), poniżej 0 — zły (czerwony). Stopa wolna od ryzyka to "
                   f"{c('portfolio.risk_free_rate_pct')}, domyślnie 0."],
        ["Maks. obsunięcie", "Największy spadek od szczytu w badanym roku, przy obecnych wagach."],
        ["Śr. korelacja", "Średnia korelacja dziennych zmian między parami pozycji (od −1 do +1). "
                          "Od 0,7 wzwyż panel ostrzega, że portfel zachowuje się jak jeden zakład."],
        ["Wsp. dywersyfikacji", "Średnia ważona zmienności pojedynczych pozycji podzielona przez zmienność "
                                "portfela. 1,0 = brak korzyści z dywersyfikacji; im wyżej, tym lepiej."],
        ["Zwrot hist. (roczny)", "Zwrot hipotetycznego portfela o obecnych wagach — NIE Twój faktyczny wynik."],
    ], [24, 76])
    s.p("Poniżej miar widać, obok siebie (na węższym ekranie jedno pod drugim): listę najsilniej "
        "skorelowanych par (para o korelacji co najmniej 0,85 również wywołuje ostrzeżenie) oraz — dla "
        "portfeli z 2–12 pozycjami — macierz korelacji w formie mapy cieplnej: im ciemniejszy bursztyn, "
        "tym silniejsza dodatnia korelacja; zielony oznacza korelację ujemną.")
    s.callout("note",
              "To hipotetyczny portfel o stałych, dzisiejszych wagach liczony na jednorocznej historii — nie "
              "Twoja faktyczna historia. Zwroty są liczone w walutach notowania, więc nie uwzględniają "
              "wpływu zmian kursów walut. Bardzo świeże spółki (mniej niż ok. 60 wspólnych sesji z resztą "
              "portfela) uniemożliwiają policzenie statystyk — panel poinformuje wtedy o powodzie. "
              "Przeszłość nie gwarantuje przyszłości.")
    s.h2("14.3 Portfel a benchmark")
    s.p("Pod miarami ryzyka w panelu „Korelacja i zmienność” znajduje się sekcja „Portfel a benchmark”. "
        "Odpowiada na pytanie, które trudno ocenić na oko: <b>czy portfel bije rynek dzięki trafnemu wyborowi "
        "spółek, czy po prostu płynie z hossą</b> — bo portfel o wysokiej becie rośnie szybciej od rynku "
        "w dobrych czasach i tak samo mocniej spada w złych.")
    s.table(["Miara", "Jak ją czytać"], [
        ["Portfel (hipotet.) / benchmark", "Zwrot w badanym okresie (zwykle ostatni rok) hipotetycznego portfela "
                                           "o obecnych wagach oraz benchmarku, z różnicą w punktach procentowych."],
        ["Beta", "Jak mocno portfel reaguje na ruchy benchmarku. 1,0 = tak samo; 1,5 = średnio o połowę mocniej "
                 "(w obie strony); poniżej 1 = łagodniej."],
        ["Alfa (rocznie)", "Część wyniku, której NIE tłumaczy sama ekspozycja na rynek (beta). Dodatnia sugeruje, "
                           "że wybór spółek dodał wartość ponad rynek; bliska zera — że wynik to głównie "
                           "ekspozycja na rynek."],
        ["Korelacja", "Jak zgodnie dzienne zmiany portfela podążają za benchmarkiem (od −1 do +1)."],
        ["Wychwyt wzrostów / spadków", "Jaką część ruchu rynku portfel łapał średnio w dni wzrostowe i w dni "
                                       "spadkowe rynku. Najkorzystniej: dużo we wzrostach, mało w spadkach."],
    ], [26, 74])
    s.p("Pod miarami widać wykres wzrostu 100 jednostek dla portfela (złota linia) i benchmarku (szara, "
        "przerywana) oraz krótkie, opisowe wnioski wynikające z liczb — np. że przewaga nad rynkiem wynika "
        "głównie z wyższej bety, a nie z selekcji spółek. Wnioski nie są rekomendacją.")
    s.p(f"<b>Wybór benchmarku:</b> domyślnie taki, jaki odpowiada dominującej walucie portfela (wg wartości "
        f"rynkowej): dla USD — SPY, dla PLN — WIG20, dla EUR — STOXX 50 (mapowanie "
        f"{c('technical.benchmark_by_currency')}). Możesz wskazać własny, np. QQQ, parametrem "
        f"{c('portfolio.benchmark')}.")
    s.callout("note",
              "Porównanie dotyczy HIPOTETYCZNEGO portfela o obecnych, stałych wagach na wspólnej historii "
              "notowań — nie Twojej faktycznej krzywej kapitału (surowy widok „Wartość” w rozdziale 14.4; "
              "widok „Zwrot (TWR)” tam obok już JEST Twoją faktyczną historią, ale to osobna, nowsza "
              "funkcja — nie ta sama liczba co tutaj). Alfa i beta z jednego roku mają duży błąd "
              "statystyczny i niekoniecznie utrzymają się w przyszłości.")
    s.h2("14.4 Krzywa kapitału")
    s.p("Przełącznik nad wykresem ma dwa wymiary: którą walutę pokazać („Łącznie” — wszystkie waluty "
        "przeliczone na walutę bazową kursem z momentu danego cyklu, albo osobno każdą natywną walutę) "
        "oraz który WIDOK narysować — „Wartość” albo „Zwrot (TWR)”.")
    s.h2("14.4.1 Widok „Wartość”")
    s.p("Wykres liniowy pokazujący, jak zmieniała się wartość Twojego portfela w czasie (linia złota) na "
        "tle zainwestowanego kosztu (linia przerywana szara). Punkt zapisywany jest przy każdym pełnym "
        "cyklu analizy, a na wykresie widać jeden punkt na dzień.")
    s.p("Etykieta nad wykresem pokazuje maksymalne historyczne obsunięcie kapitału (drawdown) — o ile "
        "procent portfel spadł od swojego dotychczasowego szczytu, wraz z datami szczytu i dołka, oraz "
        "bieżące obsunięcie, jeśli portfel akurat jest poniżej swojego historycznego maksimum.")
    s.callout("warn",
              "Dokupienie akcji PODBIJA tę linię bez żadnego realnego zysku, a sprzedaż ją ZANIŻA — to "
              "surowa wartość i koszt, nie stopa zwrotu. Do uczciwej oceny „czy zarabiam”, przełącz się na "
              "widok „Zwrot (TWR)” obok.")
    s.h2("14.4.2 Widok „Zwrot (TWR)”")
    s.p("Prawdziwy, skumulowany zwrot z inwestycji (jedna złota linia, start = 100) — w przeciwieństwie do "
        "widoku „Wartość”, dokupienie lub sprzedaż pozycji NIE podbija ani nie zaniża tej liczby. Etykieta "
        "nad wykresem pokazuje łączny zwrot procentowy za cały widoczny okres.")
    s.p("Liczone metodą Modified Dietz: między każdą parą kolejnych zdjęć krzywej narzędzie sprawdza, ile "
        "dokupiłeś lub sprzedałeś w tym okresie (na bazie dokładnych dat i kwot transakcji, które i tak już "
        "są zapisane w portfelu), waży ten przepływ liczbą dni, przez którą „pracował” w danym oknie, i "
        "liczy stąd realny zwrot okresu. Zwroty kolejnych okresów są następnie składane geometrycznie w "
        "jeden skumulowany wskaźnik.")
    s.callout("note",
              "Dla widoku „Łącznie” przepływy w walutach obcych są przeliczane przybliżonym kursem "
              "rynkowym z dnia transakcji (nie oficjalnym kursem NBP — to ocena wydajności portfela, nie "
              "rozliczenie podatkowe, więc precyzja NBP nie jest tu potrzebna). Transakcja bez dostępnego "
              "kursu jest pomijana w wyliczeniu tego jednego okresu, zamiast psuć cały wynik.")
    s.callout("tip",
              "Oba widoki („Wartość” i „Zwrot”) potrzebują punktów z co najmniej dwóch RÓŻNYCH dni, żeby "
              "narysować linię — kilka cykli tego samego dnia to na wykresie jeden punkt. Jeśli dashboard "
              "działa od niedawna, wykres będzie pusty do następnego dnia — to nie błąd.")

    # ------------------------------------------------------------ 15
    s.h1("15. Zamknięte transakcje i podsumowanie podatkowe")
    s.p("Zakładka „Zamknięte transakcje” zbiera sprzedane pozycje z pełną historią (nie usuwa danych). "
        "Pozycje trafiają tu po kliknięciu „Sprzedaj” w portfelu albo z importu raportu XTB. Przycisk ↺ "
        "przy transakcji pozwala cofnąć sprzedaż, gdyby doszło do pomyłki. Listę można sortować po "
        "spółce, cenie kupna, cenie sprzedaży i wyniku.")
    s.h2("15.1 Podsumowanie zrealizowanych wyników")
    s.p("Nad listą widoczny jest pasek z liczbą transakcji, win rate (% transakcji zamkniętych na plusie) i "
        "sumarycznym zrealizowanym zyskiem/stratą — osobno dla każdej waluty.")
    s.h2("15.2 Orientacyjne podsumowanie podatkowe")
    s.p("Nad podsumowaniem dashboard pokazuje karty pogrupowane wg roku podatkowego (rok daty sprzedaży), z "
        "sumą zysków, sumą strat, wynikiem netto oraz szacowanym podatkiem od zysków kapitałowych (19%, "
        "tzw. „podatek Belki”) liczonym od dodatniego wyniku netto w danym roku.")
    s.p("Dla waluty bazowej PLN transakcje w walutach obcych przeliczane są <b>oficjalnym średnim kursem NBP</b> "
        "z ostatniego dnia roboczego poprzedzającego dzień transakcji (osobno dla zakupu i sprzedaży), "
        "zgodnie z art. 11a ustawy o PIT. Kursy pobierane są z publicznego API NBP i zapamiętywane na stałe. "
        "Jeśli NBP jest niedostępny dla danej waluty lub daty (albo waluta bazowa jest inna niż PLN), "
        "narzędzie używa przybliżonego kursu rynkowego z Yahoo Finance i wyraźnie to zaznacza w uwagach "
        "oraz w treści ostrzeżenia pod podsumowaniem.")
    s.callout("important",
              "To NIE jest oficjalne rozliczenie podatkowe. Nawet przy kursach NBP podsumowanie nie "
              "uwzględnia m.in. prowizji, dywidend ani odliczania strat z lat poprzednich. Przed złożeniem "
              "PIT-38 zweryfikuj kwoty i skonsultuj się z doradcą podatkowym.")
    s.callout("tip",
              "Rok podatkowy i kurs NBP zależą od daty sprzedaży. Przy przycisku „Sprzedaj” data to dzień "
              "kliknięcia — jeśli transakcję wykonałeś wcześniej, dokładniejsze daty da import z raportu XTB.")
    s.h2("15.2.1 Pozycje na koncie IKE")
    s.p("Zamknięte transakcje oznaczone jako konto IKE (rozdziały 12.1 i 12.4) dostają WŁASNY, osobny zestaw "
        "kart pod tytułem „Konto IKE” — nie są wliczane do zwykłego podsumowania powyżej. Powód: polskie "
        "Indywidualne Konto Emerytalne jest zwolnione z podatku Belki TYLKO wtedy, gdy wypłata następuje po "
        "osiągnięciu wieku emerytalnego (lub przy spełnieniu innych warunków z ustawy o IKE) — wcześniejsza "
        "wypłata (tzw. zwrot) jest opodatkowana DOKŁADNIE tak samo jak konto standardowe. Dashboard nie zna "
        "Twojego wieku ani okoliczności wypłaty, więc w kolumnie „Podatek” każdej karty IKE pokazuje OBA "
        "scenariusze: 0 (jeśli wypłata kwalifikuje się do zwolnienia) lub kwotę 19% (jeśli nie) — wybór, "
        "który dotyczy Ciebie, zostaje po Twojej stronie.")
    s.callout("important",
              "To NIE jest porada, który scenariusz IKE Cię dotyczy — warunki zwolnienia (wiek, minimalny "
              "okres oszczędzania, sposób wypłaty) są zapisane w ustawie o IKE i warto je zweryfikować "
              "samodzielnie albo z doradcą podatkowym przed złożeniem deklaracji.")
    s.h2("15.3 Eksport CSV")
    s.p("Przycisk „Eksportuj CSV” przy nagłówku „Historia transakcji” pobiera WSZYSTKIE zamknięte "
        "transakcje jako plik CSV gotowy do wklejenia we własny arkusz rozliczeniowy — ticker, typ konta "
        "(standardowe/IKE), liczbę akcji, daty i ceny kupna/sprzedaży, walutę, zysk/stratę w walucie "
        "notowania, liczbę dni w portfelu, notatkę, a dodatkowo kurs i przeliczony zysk/stratę w walucie "
        "bazowej (tym samym kursem "
        "NBP co podsumowanie podatkowe z rozdziału 15.2 — obie liczby zawsze się zgadzają, bo liczy je "
        "dokładnie ten sam kod). Plik używa średnika jako separatora (domyślny w polskich ustawieniach "
        "Excela) i ma dopisany znacznik kodowania, żeby polskie znaki wyświetliły się poprawnie po "
        "otwarciu.")

    # ------------------------------------------------------------ 16
    s.h1("16. Alerty cenowe i log na żywo")
    s.h2("16.1 Alerty cenowe")
    s.p("Niezależnie od pełnego, godzinnego cyklu analizy, dashboard co kilka minut (domyślnie co 3 minuty, "
        f"parametr {c('web.price_alert_interval_seconds')}) sprawdza SAMĄ CENĘ każdej otwartej pozycji w "
        "portfelu — szybki, lekki test bez angażowania lokalnego modelu ani newsów. Alert pojawia się w "
        "logu na żywo (wyróżniony kolorem i symbolem dzwonka), gdy:")
    s.table(["Alert", "Warunek"], [
        ["Poniżej stop-lossu (czerwony)", "Cena spadła poniżej stop-lossu pozycji — własnego, jeśli go "
                                          "ustawiłeś, w przeciwnym razie sugerowanego (2×ATR z ostatniego "
                                          "pełnego cyklu)."],
        ["Cena docelowa analityków (zielony)", "Cena osiągnęła średnią cenę docelową analityków Wall Street "
                                               "dla tej spółki."],
        ["Twój cel cenowy (zielony)", "Cena osiągnęła własny cel ustawiony przy pozycji."],
    ], [30, 70])
    s.p("Ten sam alert nie jest powtarzany w kółko: po zgłoszeniu milczy, dopóki sytuacja się nie zmieni "
        "(np. cena wróci nad stop-loss), po czym ponowne przekroczenie wyśle go znowu.")
    s.p("Pętla sprawdzająca ceny czeka pełen interwał PRZED pierwszym sprawdzeniem po starcie serwera (a "
        "potem między kolejnymi), więc zaraz po zmianie stop-lossu/celu albo po restarcie dashboardu na "
        "wynik trzeba by czekać do 3 minut. Przycisk „Sprawdź alerty teraz” (z ikoną dzwonka; zakładka "
        "Portfel, „Ryzyko i ekspozycja”) uruchamia dokładnie tę samą logikę natychmiast, bez czekania na "
        "najbliższy tick pętli.")
    s.h2("16.2 Log na żywo")
    s.p("To zwijana szufladka na dole ekranu — kliknij jej nagłówek, żeby ją rozwinąć lub zwinąć; stan "
        "jest zapamiętywany między sesjami. Widać w niej strumień zdarzeń z bieżącej i poprzednich analiz "
        "(jaka spółka jest właśnie analizowana, ostrzeżenia, błędy pobierania danych), alerty cenowe, "
        "zmiany kategorii spółek z watchlisty (np. nowy sygnał „WARTO OBSERWOWAĆ”) oraz sygnały "
        "AT_TOP/SHARP_DECLINE dla spółek z Twojego portfela. Kolory linii: biały — informacja, "
        "bursztynowy — ostrzeżenie, czerwony — błąd, zielony — sukces. Log jest pierwszym miejscem, do "
        "którego warto zajrzeć, gdy coś nie działa (np. nieprawidłowy ticker).")
    s.bullets([
        "Gdy szufladka jest ZWINIĘTA, każdy alert (rozpoznawany po symbolu dzwonka) podbija czerwoną, "
        "pulsującą odznakę z licznikiem na przycisku szufladki — inaczej alert mógłby przejść zupełnie "
        "niezauważony. Odznaka znika po rozwinięciu szufladki.",
        "Linie alertów DOTYCZĄCE konkretnej spółki są klikalne — klik otwiera od razu wykres tej spółki "
        "(rozdział 8), z narysowanymi liniami stop-lossu i celu (rozdział 8.1), więc widać dosłownie, "
        "DLACZEGO alert poleciał.",
    ])
    s.h2("16.3 Powiadomienia Discord (opcjonalnie)")
    s.p("Dashboard NIE wysyła natywnych powiadomień systemowych przeglądarki (funkcja ta została usunięta "
        "po tym, jak okazała się niedziałająca w niektórych przeglądarkach) — ale odznaka na szufladce "
        "(rozdział 16.2) działa tylko wtedy, gdy karta z dashboardem jest w ogóle otwarta. Żeby dostać "
        "alert także wtedy, gdy dashboard jest zamknięty, można podpiąć webhook Discorda.")
    s.steps([
        "Na serwerze Discord: Ustawienia serwera → Integracje → Webhooki → Nowy webhook, skopiuj URL "
        "(nie trzeba zakładać bota ani niczego autoryzować z poziomu tej aplikacji).",
        f"W {c('config.yaml')} ustaw {c('notifications.enabled: true')} i wklej URL do "
        f"{c('notifications.discord_webhook_url')}.",
        "Zrestartuj serwer dashboardu.",
    ])
    s.p("Przycisk „Testuj Discord” (z ikoną probówki), obok „Sprawdź alerty teraz” w zakładce Portfel, "
        "wysyła jedną testową wiadomość — pozwala od razu sprawdzić, czy webhook działa, bez czekania na "
        "prawdziwy alert. Wiadomości na Discordzie są kolorowane tak samo jak w aplikacji (czerwony "
        "stop-loss, zielony/cyjan cel).")
    s.callout("note",
              "Wyłączone domyślnie ({}). Zła konfiguracja (pusty albo nieprawidłowy URL) nigdy nie "
              "przerywa działania samych alertów w aplikacji — najwyżej powiadomienie na Discordzie się "
              "nie wyśle, o czym poinformuje log serwera.".format(c("notifications.enabled: false")))


def part3(s: Story) -> None:
    # ------------------------------------------------------------ 17
    s.h1("17. Codzienny brief AI")
    s.p("Na samej górze zakładki Analiza, po każdym pełnym cyklu, pojawia się wyróżniona, złota karta "
        "„Brief dnia” — krótki (120–180 słów), spójny akapit napisany przez lokalny model językowy, "
        "łączący w jedną narrację: kontekst makro, najważniejsze spółki „WARTO OBSERWOWAĆ” i „UNIKAJ” z "
        "dzisiejszego cyklu, koncentrację sektorową watchlisty, zbliżające się wyniki kwartalne oraz stan "
        "i ryzyko Twojego portfela.")
    s.p("To podsumowanie ma pomóc szybko zorientować się w sytuacji bez przeglądania wszystkich kart z "
        "osobna — nie zastępuje ich jednak, tylko daje punkt wyjścia. Brief generowany jest wyłącznie "
        "na końcu PEŁNEGO cyklu (nie przy odświeżeniu pojedynczej spółki). Jeśli lokalny model językowy jest "
        "wyłączony lub chwilowo niedostępny, karta briefu po prostu się nie pojawia (reszta dashboardu "
        "działa bez zmian).")

    # ------------------------------------------------------------ 18
    s.h1("18. Asystent czatu")
    s.p("Okrągły przycisk w prawym dolnym rogu ekranu otwiera okno czatu z asystentem opartym o ten sam "
        "lokalny model językowy. Możesz pytać po polsku, np. „które spółki mają dziś kategorię WARTO "
        "OBSERWOWAĆ?”, „jakie jest ryzyko mojego portfela?”, „kiedy najbliższe wyniki moich spółek?”.")
    s.bullets([
        "Asystent odpowiada WYŁĄCZNIE na podstawie danych z dashboardu: wyników analizy (posortowanych wg "
        "wyniku), briefu dnia, kontekstu makro, koncentracji sektorowej, portfela per waluta, ryzyka, "
        "ekspozycji sektorowej portfela, podsumowania podatkowego i statystyk skuteczności. Dane są "
        "pobierane na świeżo przy każdym pytaniu z ostatniego cyklu.",
        "Nie udziela porad „kup” / „sprzedaj” — opisuje, co pokazują dane i jakie sygnały wygenerowało "
        "narzędzie. Jeśli o czymś nie ma danych (spółka spoza listy, wydarzenie po ostatnim cyklu), "
        "powinien to powiedzieć wprost.",
        "Historia rozmowy jest trzymana tylko w otwartej karcie przeglądarki (znika po odświeżeniu "
        "strony); do modelu trafia ostatnich 8 wiadomości.",
        f"Wymaga włączonego modelu ({c('llm.enabled: true')}) i uruchomionej Ollamy. Odpowiedź przy dużej "
        "watchliście może potrwać dłużej — limit czasu to domyślnie 90 sekund.",
    ])
    s.callout("tip",
              "Czat dostaje naraz dużo danych. Jeśli przy większej watchliście odpowiedzi stają się "
              f"chaotyczne, zwiększ {c('llm.chat_num_ctx')} (domyślnie 8192) kosztem szybkości albo użyj "
              "większego modelu. Mały model może się mylić — ważne liczby sprawdź w kartach dashboardu.")

    # ------------------------------------------------------------ 19
    s.h1("19. Odświeżanie danych i pamięć podręczna")
    s.p("Dashboard aktualizuje dane na trzy sposoby:")
    s.steps([
        "<b>Pełny cykl automatyczny</b> — co ustaloną liczbę minut (domyślnie 60) serwer sam analizuje całą "
        "watchlistę, wykrywa nowe propozycje AI, zapisuje punkt krzywej kapitału, generuje brief dnia i "
        "odświeża panele skuteczności oraz koncentracji sektorowej. Licznik czasu do kolejnego cyklu "
        "widoczny jest w prawym górnym rogu.",
        "<b>Pełny cykl ręczny</b> — przycisk „Odśwież teraz” wymusza natychmiastowe uruchomienie tego "
        "samego pełnego cyklu, niezależnie od harmonogramu.",
        "<b>Odświeżenie pojedynczej spółki</b> — przycisk „Odśwież tę spółkę” w oknie szczegółów pobiera "
        "świeże ceny, sentyment i fundamenty tylko dla tej jednej spółki, bez czekania na cały cykl. "
        "Nic nie odświeża się samo przy otwarciu okna — to Twoja świadoma decyzja.",
    ])
    s.callout("note",
              "Odświeżanie pojedynczej spółki jest chronione krótkim odstępem czasu (domyślnie 45 sekund) "
              "między kolejnymi żądaniami dla tej samej spółki — to zabezpieczenie przed przeciążeniem "
              "zewnętrznych, darmowych źródeł danych (Yahoo Finance, Finnhub). Lokalny model językowy nie ma "
              "takiego ograniczenia, bo działa offline. Odświeżenie pojedynczej spółki nie stosuje "
              "podwyższenia progu z reżimu makro, które stosuje pełny cykl, więc kategoria takiej spółki "
              "może chwilowo różnić się od tej po pełnym cyklu.")
    s.h2("19.1 Pamięć podręczna (dlaczego kolejne cykle są szybsze)")
    s.table(["Dane", "Jak długo są pamiętane"], [
        ["Ceny notowań (yfinance)", "5 minut w pamięci procesu. Alerty cenowe zawsze pobierają cenę na "
                                    "świeżo, z pominięciem tej pamięci."],
        ["Benchmark (np. SPY, WIG20)", "1 godzina — jeden pobór wspólny dla wielu spółek w tym samym cyklu."],
        ["Kursy walut — bieżące", "15 minut (pamięć oraz dysk, przetrwa restart serwera)."],
        ["Kursy walut — historyczne i NBP", "Bezterminowo na dysku — kurs z przeszłej daty się nie zmienia."],
        ["Newsy historyczne (Finnhub)", "Miesiące zakończone — bezterminowo na dysku, pobierane tylko raz. "
                                        "Tylko bieżący miesiąc jest odpytywany w każdym cyklu."],
        ["Termin wyników kwartalnych", "12 godzin w pamięci procesu."],
    ], [32, 68])
    s.p("Dzięki temu pierwszy cykl po instalacji jest najwolniejszy („rozgrzewający”), a kolejne — wyraźnie "
        "krótsze. Znacznik wieku danych przy każdej karcie (ikona zegara) pokazuje, kiedy dana spółka była "
        "ostatnio analizowana; kolor bursztynowy sygnalizuje dane starsze niż 2 godziny, co zwykle oznacza, "
        "że coś zawiodło w automatycznym cyklu.")

    # ------------------------------------------------------------ 20
    s.h1("20. Kopia zapasowa i dane lokalne")
    s.h2("20.1 Gdzie są Twoje dane")
    s.bullets([
        f"{c('data/xtb_trend_watch.db')} — baza SQLite: watchlista, portfel (otwarte i zamknięte pozycje), "
        "historia werdyktów, krzywe kapitału, pamięć podręczna newsów i kursów. Wszystko zostaje na Twoim "
        "komputerze.",
        f"{c('config.yaml')} — Twoja konfiguracja, w tym klucz API Finnhub. Nie udostępniaj go nikomu i "
        "nie wysyłaj do repozytorium.",
        f"{c('reports/')} — raporty z trybu terminalowego (.md i .json).",
    ])
    s.h2("20.2 Eksport i import kopii zapasowej")
    s.p("W zakładce Portfel, w sekcji „Kopia zapasowa”, przycisk „Pobierz kopię (JSON)” zapisuje plik "
        "z watchlistą, całym portfelem i dywidendami. Aby wczytać kopię (np. po przeniesieniu na nowy "
        "komputer), wybierz plik i kliknij „Wczytaj kopię”.")
    s.table(["Wchodzi do kopii", "NIE wchodzi do kopii"], [
        ["Watchlista (ticker, nazwa, symbol XTB).", "Konfiguracja config.yaml (przenieś osobno)."],
        ["Otwarte i zamknięte pozycje portfela wraz z notatkami i walutą.", "Historia werdyktów i wykresy "
                                                                            "znaczników."],
        ["Własne poziomy stop-loss i cel cenowy.", "Krzywe kapitału (odbudują się z kolejnych cykli)."],
        ["Notatki do pozycji (razem ze znacznikami importu XTB).",
         "Wyniki analizy i pamięć podręczna newsów/kursów (odbudują się automatycznie)."],
        ["Dywidendy (rozdział 12.6) — kwota, waluta, data, podatek u źródła.", ""],
    ], [50, 50])
    s.bullets([
        "Wczytanie jest bezpieczne do powtarzania: identyczne pozycje, spółki i dywidendy są pomijane, więc "
        "ponowny import tego samego pliku nie tworzy duplikatów.",
        "Niepoprawne wiersze są pomijane z ostrzeżeniem (widocznym w komunikacie i w logu), a reszta pliku "
        "jest wczytywana. Plik, który nie jest kopią XTB Trend Watch, zostanie odrzucony w całości. "
        "Limit to 5 MB i 5000 wierszy.",
        "Spółki występujące w pozycjach portfela są automatycznie dopisywane do watchlisty, żeby były "
        "analizowane.",
    ])

    # ------------------------------------------------------------ 21
    s.h1("21. Ograniczenia i najczęstsze błędy interpretacyjne")
    s.bullets([
        "<b>To nie jest system uczący się.</b> Wagi scoringu i logika reguł są stałe — panel skuteczności "
        "pokazuje statystyki, ale nic z nimi automatycznie nie robi. Ewentualne korekty progów wymagają "
        "ręcznej zmiany konfiguracji. Lokalny model nie ma pamięci między wywołaniami.",
        "<b>Sentyment LLM jest orientacyjny.</b> Mały, lokalny model może różnie oceniać podobne newsy w "
        "różnych cyklach. W teście na ok. 24 tys. historycznych nagłówków z dwóch spółek ocena "
        "sentymentu pojedynczego nagłówka (także uśredniona dziennie) nie wykazała związku z późniejszą "
        "zmianą ceny — traktuj ją jako kontekst do przeczytania, nie jako sygnał predykcyjny.",
        "<b>Dane spółek spoza głównych giełd USA bywają niepełne.</b> Newsy historyczne z Finnhub w darmowym "
        "planie zwykle nie obejmują np. spółek z GPW. Dla tickerów z sufiksem .WA narzędzie zamiast tego "
        "buduje własny trend, dopisując przy każdym cyklu trafienia z ogólnego kanału RSS o GPW "
        "(news.gpw_rss_url) do lokalnego cache'u — trend zacznie się jednak pojawiać dopiero od momentu "
        "włączenia tej funkcji, nie wstecz, więc brak trendu przy świeżo dodanej spółce z GPW jest normalny, "
        "nie błędem.",
        "<b>Cena docelowa analityków to zewnętrzna opinia rynkowa</b>, nie własna wycena narzędzia — "
        "analitycy też się mylą i bywają opóźnieni względem najnowszych wydarzeń. Analiza fundamentalna to "
        "szybki zestaw wskaźników, nie pełna wycena spółki (DCF).",
        "<b>Backtest i optymalizacja parametrów</b> dotyczą wyłącznie logiki technicznej, nie uwzględniają "
        "kosztów i mogą odzwierciedlać wyjątkowy reżim rynkowy (rozdział 9).",
        "<b>Statystyki portfela (korelacje, zmienność, Sharpe, porównanie z benchmarkiem)</b> liczone są dla hipotetycznego portfela o "
        "obecnych wagach na rocznej historii, w walutach notowania — to obraz ryzyka, nie prognoza.",
        "<b>Daty wyników kwartalnych z Yahoo bywają szacunkowe</b> i mogą się przesunąć.",
        "<b>Panel skuteczności potrzebuje czasu.</b> Świeżo dodana spółka lub świeżo uruchomiony dashboard "
        "nie mają jeszcze wystarczającej historii do wiarygodnych statystyk; historia z backfillu opiera "
        "się tylko na analizie technicznej.",
        "<b>Yahoo Finance to nieoficjalne, publiczne API</b> — bywa niestabilne lub zwraca niepełne dane; "
        "narzędzie ponawia próby, ale chwilowe braki się zdarzają. Kontekst makro nie obejmuje inflacji "
        "(brak w pełni darmowego, aktualnego źródła).",
        "<b>Indeks ^WIG20 regularnie bywa niedostępny w Yahoo Finance.</b> Narzędzie wykrywa to automatycznie "
        "i sięga po zastępcze źródło (domyślnie darmowe notowania ze Stooq, a w razie potrzeby ETF "
        "ETFBW20TR.WA) — siła względna spółek z GPW nadal się liczy, tylko z adnotacją, z jakiego źródła "
        "pochodzi (widoczną w powodach analizy technicznej). Gdy WSZYSTKIE źródła danego benchmarku zawiodą, "
        "informacja trafia do logu na żywo, a narzędzie nie ponawia prób co spółkę — czeka kilka minut, żeby "
        "nie marnować czasu cyklu.",
    ])
    s.callout("important",
              "Żadna kategoria, wynik ani rekomendacja generowana przez to narzędzie nie stanowi porady "
              "inwestycyjnej. Zawsze podejmuj decyzje na podstawie własnej, niezależnej analizy.")

    # ------------------------------------------------------------ 22
    s.h1("22. Konfiguracja (dla zaawansowanych)")
    s.p(f"Zachowanie narzędzia można dostroić w pliku {c('config.yaml')} (wzorzec: {c('config.example.yaml')}). "
        "Najczęściej modyfikowane parametry:")
    s.table(["Parametr", "Domyślnie", "Opis"], [
        ["scoring.suggestion_threshold", "0.55", "Próg wyniku łącznego, powyżej którego spółka trafia do "
                                                 "sekcji pozytywnych kategorii."],
        ["scoring.technical_weight / sentiment_weight / fundamentals_weight", "0.55 / 0.30 / 0.15",
         "Wagi składników wyniku łącznego (suma = 1)."],
        ["technical.rsi_oversold", "30", "Próg RSI wyprzedania (wcześniej 35; patrz rozdział 9)."],
        ["technical.sharp_decline_threshold_pct", "15", "Spadek ceny (w %) w oknie kilku sesji uznawany za "
                                                        "„gwałtowny spadek”."],
        ["technical.atr_stop_multiplier", "2.0", "Mnożnik ATR w sugerowanym stop-lossie."],
        ["technical.benchmark_by_currency", "USD: SPY, PLN: ^WIG20, EUR: ^STOXX50E",
         "Benchmark do siły względnej, zależny od waluty spółki."],
        ["technical.benchmark_fallbacks", "^WIG20: [stooq:wig20, ETFBW20TR.WA]",
         "Zastępcze źródła danego benchmarku, próbowane po kolei, gdy główne źródło (Yahoo Finance) zawiedzie "
         "(rozdział 21). Pusta lista wyłącza zastępniki dla danego wpisu."],
        ["technical.use_weekly_confirmation", "true", "Potwierdzanie sygnału dziennego trendem tygodniowym."],
        ["macro.vix_elevated_threshold / vix_high_threshold", "20 / 30", "Progi reżimów PODWYŻSZONY i WYSOKI."],
        ["macro_calendar.days_ahead", "14", "Okno (w dniach), w którym wydarzenia z macro_calendar.events "
                                            "pojawiają się jako „Nadchodzące wydarzenia” (rozdział 4.1)."],
        ["fundamentals.earnings_warning_days", "7", "Ile dni przed wynikami kwartalnymi pokazywać "
                                                    "ostrzeżenie."],
        ["news.use_historical_news / finnhub_api_key", "true / —",
         "Newsy historyczne z Finnhub. Bez prawidłowego klucza ustaw false, inaczej cykl marnuje czas na "
         "nieudane zapytania."],
        ["news.gpw_rss_url", "Bankier.pl — Giełda", "Kanał RSS uzupełniający sentyment spółek z GPW "
                                                    "(rozdział 21). Pusty string wyłącza to źródło."],
        ["llm.enabled / llm.model", "true / llama3.1:8b", "Włączenie lokalnego modelu i wybór modelu."],
        ["llm.chat_num_ctx", "8192", "Rozmiar kontekstu czatu (tokeny)."],
        ["discovery.enabled / cooldown_days / max_candidates", "true / 14 / 5",
         "Propozycje AI: włączenie, dni przerwy przed ponowną propozycją, maks. liczba kandydatów."],
        ["web.refresh_interval_minutes", "60", "Co ile minut uruchamia się automatyczny pełny cykl."],
        ["web.manual_refresh_cooldown_seconds", "45", "Minimalny odstęp między ręcznymi odświeżeniami tej "
                                                      "samej spółki."],
        ["web.price_alert_interval_seconds", "180", "Co ile sekund sprawdzać samą cenę pozycji "
                                                    "(0 = alerty wyłączone)."],
        ["notifications.enabled / discord_webhook_url", "false / brak", "Powiadomienia o alertach cenowych "
                                                                        "na Discordzie (rozdział 16.3)."],
        ["portfolio.base_currency", "PLN", "Waluta bazowa podsumowania łącznego, krzywej kapitału i "
                                           "statystyk portfela."],
        ["portfolio.risk_free_rate_pct", "0.0", "Stopa wolna od ryzyka (% rocznie) we współczynniku Sharpe’a i w alfie."],
        ["portfolio.benchmark", "brak (auto)", "Własny benchmark do porównania z portfelem (np. QQQ). Bez wartości "
                                              "wybierany wg dominującej waluty portfela."],
        ["effectiveness.min_signal_age_days", "14", "Ile dni musi minąć od werdyktu, by wliczyć go do "
                                                    "panelu skuteczności."],
    ], [46, 17, 37], mono_first_col=True)
    s.callout("tip",
              "Po każdej zmianie w config.yaml należy zrestartować serwer (zatrzymać i uruchomić ponownie "
              "polecenie z rozdziału 2), żeby zmiany zaczęły obowiązywać.")

    # ------------------------------------------------------------ 23
    s.h1("23. Rozwiązywanie problemów")
    s.table(["Objaw", "Najczęstsza przyczyna i co zrobić"], [
        ["Spółka nie pojawia się w wynikach", "Sprawdź log na żywo — najczęściej to nieprawidłowy ticker "
                                              "(format Yahoo Finance, np. ALE.WA). Nowa spółka pojawia się "
                                              "dopiero po najbliższym cyklu."],
        ["Krzywa kapitału pusta", "Potrzebne są punkty z co najmniej dwóch różnych dni (rozdział 14.4). "
                                  "Widok „Łącznie” wymaga ponadto kursów walut — szukaj ostrzeżenia w logu."],
        ["Brak briefu dnia albo czat nie odpowiada", "Sprawdź, czy Ollama działa (ollama serve), czy model "
                                                     "jest pobrany i czy llm.enabled: true. Brief pojawia "
                                                     "się dopiero po zakończeniu pełnego cyklu."],
        ["Brak trendu sentymentu 12-miesięcznego", "Newsy historyczne wymagają klucza Finnhub, a spółki z GPW "
                                                   "zwykle nie są objęte darmowym planem — to normalne."],
        ["Komunikat „Poczekaj jeszcze N s”", "Działa ochrona przed zbyt częstym odświeżaniem tej samej "
                                             "spółki (domyślnie 45 s). Odczekaj podaną liczbę sekund."],
        ["Panel „Korelacja i zmienność” mówi o zbyt małej historii", "W portfelu jest bardzo świeża spółka "
                                                                     "(poniżej ok. 60 wspólnych sesji z "
                                                                     "resztą). Statystyki policzą się, gdy "
                                                                     "uzbiera się historia."],
        ["Brak alertów cenowych", "Alerty dotyczą wyłącznie otwartych pozycji z portfela i wymagają "
                                  "web.price_alert_interval_seconds &gt; 0. Alert stop-lossu wyzwala się dopiero, gdy "
                                  "cena spadnie PONIŻEJ tego poziomu, a alert celu — gdy cena go "
                                  "osiągnie; każdy jest zgłaszany raz."],
        ["Ostrzeżenia po imporcie z XTB", "Nierozpoznany sufiks giełdy — popraw ticker przyciskiem ✎ "
                                          "przy pozycji (format Yahoo Finance)."],
        ["Wykres świecowy się nie ładuje", "Biblioteka wykresów ładowana jest z internetu — sprawdź "
                                           "połączenie. Dane cenowe pochodzą z Yahoo Finance."],
        ["Dane spółki są „stare” (bursztynowy znacznik wieku)", "Automatyczny cykl mógł się nie wykonać — "
                                                                "sprawdź log, a w razie potrzeby użyj "
                                                                "„Odśwież teraz”."],
    ], [30, 70])

    # ------------------------------------------------------------ 24
    s.h1("24. Historia zmian dokumentu")
    s.p("Ten dokument jest aktualizowany wraz z rozwojem narzędzia. Poniższa tabela śledzi kolejne wersje.")
    s.table(["Wersja", "Data", "Zmiany"], [
        ["1.0", "Sierpień 2026", "Pierwsza wersja instrukcji — obejmuje zakładki Analiza i Portfel, panel "
                                 "skuteczności, koncentrację sektorową, propozycje AI, kalkulator wielkości "
                                 "pozycji, powiadomienia i odświeżanie na żywo."],
        ["2.0", "Wrzesień 2026", "Rozdziały o walutach i podsumowaniu łącznym portfela, panelu ryzyka i "
                                 "krzywej kapitału (z max drawdown), zamkniętych transakcjach i "
                                 "orientacyjnym podsumowaniu podatkowym (PIT-38), alertach cenowych "
                                 "niezależnych od cyklu oraz codziennym briefie AI. Usunięto rozdział o "
                                 "powiadomieniach przeglądarki (funkcja wycofana jako niedziałająca w "
                                 "niektórych przeglądarkach)."],
        ["3.0", "Wrzesień 2026", "Dokument przepisany i rozszerzony o stan aktualny narzędzia. Nowe "
                                 "rozdziały: wyniki kwartalne i ostrzeżenia o zmienności, asystent czatu, "
                                 "kopia zapasowa i dane lokalne, rozwiązywanie problemów. Nowe funkcje "
                                 "opisane: własny stop-loss i cel cenowy per pozycja, panel „Korelacja i "
                                 "zmienność”, import z raportu XTB, miniwykresy i znacznik wieku danych, "
                                 "siła względna vs benchmark, obsługa ETF-ów, oficjalny kurs NBP w "
                                 "podsumowaniu podatkowym, pamięć podręczna, backtest walk-forward i "
                                 "backfill, log na żywo jako zwijana szufladka, ręczne odświeżanie "
                                 "pojedynczej spółki (zamiast automatycznego). Poprawiono opisy: alerty "
                                 "cenowe trafiają do logu, przycisk edycji pozycji działa, krzywa kapitału "
                                 "wymaga dwóch różnych dni, RSI wyprzedania 30."],
        ["3.1", "Wrzesień 2026", "Nowy podrozdział 14.3 „Portfel a benchmark”: porównanie hipotetycznego portfela o "
                                 "obecnych wagach z benchmarkiem (zwrot, beta, alfa, korelacja, wychwyt wzrostów "
                                 "i spadków, wykres, wnioski) oraz parametr portfolio.benchmark. Krzywa kapitału "
                                 "przeniesiona do 14.4."],
        ["3.2", "Wrzesień 2026", "Automatyczne zastępcze źródło benchmarku (np. Stooq dla ^WIG20), gdy główne "
                                 "źródło jest niedostępne w Yahoo Finance — opisane w rozdziałach 6.3, 21 i 22 "
                                 "(nowy parametr technical.benchmark_fallbacks)."],
        ["3.3", "Wrzesień 2026", "Eksport CSV zamkniętych transakcji (15.3). Prawdziwa (TWR) krzywa kapitału "
                                 "jako drugi widok obok surowej wartości (14.4.2, metoda Modified Dietz). "
                                 "Stop-loss/cel zawsze widoczne na kartach portfela (12.5) i narysowane jako "
                                 "linie na wykresie spółki (8.1, z nowymi, nienakładającymi się kolorami: SMA "
                                 "200 fioletowa, cel cyjanowy). Przycisk ręcznego sprawdzenia alertów i odznaka "
                                 "z licznikiem na szufladce logu, klikalne linie alertów (16.1–16.2). Nowy "
                                 "rozdział 16.3: opcjonalne powiadomienia o alertach na Discordzie (webhook)."],
        ["3.4", "Wrzesień 2026", "Uwzględnienie rzeczywistego kursu wymiany brokera (np. marża XTB) w "
                                 "podsumowaniu łącznym portfela i krzywej TWR: opcjonalne pole przy ręcznym "
                                 "dodawaniu pozycji (12.1) oraz automatyczne wyliczenie i retrospektywny "
                                 "backfill przy imporcie z XTB (12.4); zaktualizowano opis podsumowania "
                                 "łącznego (13.1). Alternatywne źródło sentymentu dla spółek z GPW: filtrowany "
                                 "kanał RSS (news.gpw_rss_url) budujący własny trend 12-miesięczny w czasie, "
                                 "zamiast trwałego braku danych przez 403 z Finnhuba (rozdziały 21, 22)."],
        ["3.5", "Wrzesień 2026", "Przycisk „Sprzedaj” pyta teraz opcjonalnie także o rzeczywisty kurs wymiany "
                                 "brokera przy sprzedaży (12.3), analogicznie do pola przy dodawaniu pozycji "
                                 "(12.1) — domyka rzeczywisty kurs brokera po obu stronach transakcji."],
        ["3.6", "Wrzesień 2026", "Naprawiono import z XTB (12.4): pozycja zaimportowana wcześniej jako "
                                 "otwarta, a od tego czasu sprzedana u brokera, jest teraz przy ponownym "
                                 "imporcie automatycznie zamykana w dashboardzie zamiast zostawać otwartą na "
                                 "zawsze (import rozpoznawał ten sam numer pozycji jako duplikat i tylko "
                                 "dogrywał kurs, nigdy nie zmieniając statusu)."],
        ["3.7", "Wrzesień 2026", "Nowy rozdział 12.6: śledzenie dywidend — przychód brutto/netto per "
                                 "wypłata i podsumowanie wg roku, wczytywane automatycznie przy imporcie "
                                 "XTB (z arkusza Cash Operations) albo dodawane ręcznie. Kopia zapasowa "
                                 "(20.2) obejmuje teraz też dywidendy."],
        ["3.8", "Wrzesień 2026", "Kalendarz najbliższych wydarzeń makro (FOMC, RPP/NBP, CPI USA) w panelu "
                                 "„Kontekst makro” (4.1), konfigurowalny w config.yaml. Heatmapa całej "
                                 "watchlisty jako alternatywa dla widoku kart (5), kolorowana wg zmiany "
                                 "ceny w ostatniej sesji."],
        ["3.9", "Wrzesień 2026", "Nowy rozdział 3.1: zwijalne sekcje głównej kolumny na WSZYSTKICH "
                                 "zakładkach (nie tylko panele boczne jak dotąd) - każda większa sekcja "
                                 "(Ryzyko, Korelacja, Krzywa kapitału, Historia transakcji itd.) ma teraz "
                                 "własny przycisk zwijania. Personalizacja panelu bocznego (przeciąganie, "
                                 "zwijanie) działa też w zakładce Portfel, nie tylko w Analizie."],
        ["3.10", "Wrzesień 2026", "Poprawki układu: „Najsilniej skorelowane pary” i „Macierz korelacji” "
                                  "(14.2) stoją teraz obok siebie zamiast na pełną szerokość; karty "
                                  "poszczególnych lat w podsumowaniu podatkowym (15.2) i dywidendach (12.6) "
                                  "też stoją obok siebie, gdy jest ich kilka. Poprawiono też odstępy między "
                                  "sąsiadującymi blokami szczegółów w kilku panelach."],
        ["3.11", "Wrzesień 2026", "Flaga „konto IKE” per pozycja (12.1) — rozpoznawana automatycznie przy "
                                  "imporcie XTB (12.4) albo ustawiana ręcznym checkboxem. Nowy rozdział "
                                  "15.2.1: pozycje IKE liczone są w podsumowaniu podatkowym osobno, z dwoma "
                                  "scenariuszami podatku (0 lub 19%), bo zależą od wieku przy wypłacie, "
                                  "którego narzędzie nie zna. Eksport CSV (15.3) ma nową kolumnę „Konto”."],
    ], [10, 18, 72])
    s.p("<i>Koniec dokumentu. W razie pytań dotyczących działania konkretnej funkcji, sprawdź odpowiedni "
        f"rozdział powyżej lub skonsultuj plik config.yaml i log na żywo.</i>")


def content() -> Story:
    s = Story()
    part1(s)
    part2(s)
    part3(s)
    return s


def main() -> None:
    parser = argparse.ArgumentParser(description="Generuje PDF instrukcji użytkownika XTB Trend Watch")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent
                                             / "XTB_Trend_Watch_Instrukcja_Uzytkownika.pdf"))
    args = parser.parse_args()
    build(content(), args.out)
    print(f"Zapisano: {args.out}")


if __name__ == "__main__":
    main()
