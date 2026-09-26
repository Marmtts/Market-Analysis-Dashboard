// ============================================================
// XTB Trend Watch — Terminal — logika frontendu (vanilla JS)
// ============================================================

const state = {
  ws: null,
  wsReconnectDelay: 1500,
  nextRunAt: null,
  chart: null,
  candleSeries: null,
  ma50Series: null,
  ma200Series: null,
  resultsByTicker: new Map(),
  lastPayload: null,
  openTicker: null,
  categoryByTicker: new Map(),
  portfolioTickers: new Set(),
  // Personalizacja kolumn watchlisty/propozycji AI (patrz loadColumnPreferences) -
  // wartość domyślna tu tylko na wypadek, gdyby coś odczytało state.columnPrefs
  // zanim init() zdąży wczytać localStorage; loadColumnPreferences() i tak
  // nadpisuje ten obiekt zaraz na starcie.
  columnPrefs: { target: true, signal: true, score: true, chart: true },
  // Licznik alertów (🔔 w logu), których użytkownik jeszcze nie widział, bo
  // szuflada logu była zwinięta - patrz appendLog/updateLogDrawerBadge.
  // Bez tego alert cenowy/sygnałowy mógł przejść zupełnie niezauważony po
  // usunięciu (niedziałających w Brave) natywnych powiadomień przeglądarki.
  unseenAlertCount: 0,
};

const el = (id) => document.getElementById(id);

// ---------------- Kategoria -> klasa/etykieta pieczęci ----------------
function stampInfo(category) {
  if (category.startsWith("WARTO OBSERWOWAĆ")) return { cls: "buy", label: "WARTO\nOBSERWOWAĆ" };
  if (category.startsWith("NEUTRALNIE")) return { cls: "watch", label: "NEUTRALNIE" };
  if (category.startsWith("UNIKAJ")) return { cls: "avoid", label: "UNIKAJ" };
  return { cls: "wait", label: "CZEKAJ" };
}

// ---------------- Terminy wyników kwartalnych ----------------
function getEarningsWarningDays() {
  return (state.lastPayload && state.lastPayload.earnings_warning_days) ?? 7;
}

// Liczba dni od DZIŚ do daty "YYYY-MM-DD" (liczona po stronie przeglądarki,
// żeby nie starzała się w cache'u). Zwraca null dla braku daty lub terminu w przeszłości.
function daysUntilDate(dateStr) {
  if (!dateStr) return null;
  const target = new Date(`${dateStr}T00:00:00`);
  if (isNaN(target.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((target - today) / 86400000);
  return days >= 0 ? days : null;
}

function earningsOf(r) {
  const f = r && r.fundamentals;
  if (!f || !f.available) return null;
  const date = f.metrics && f.metrics.next_earnings_date;
  const days = daysUntilDate(date);
  return days == null ? null : { date, days };
}

function earningsWhenLabel(days) {
  if (days === 0) return "dzisiaj";
  if (days === 1) return "jutro";
  return `za ${days} dni`;
}

function earningsBadgeHtml(date, days) {
  if (!date || days == null || days > getEarningsWarningDays()) return "";
  return `<span class="earnings-badge" title="Wyniki kwartalne ${escapeHtml(date)} - możliwa duża zmiana ceny w obie strony">📅 wyniki ${earningsWhenLabel(days)}</span>`;
}

function earningsBannerHtml(r) {
  const e = earningsOf(r);
  if (!e || e.days > getEarningsWarningDays()) return "";
  return `<div class="detail-section earnings-banner">📅 <strong>Wyniki kwartalne ${earningsWhenLabel(e.days)} (${escapeHtml(e.date)}).</strong> Publikacja często powoduje gwałtowny ruch ceny w obie strony, także przy dobrych liczbach. Zastanów się nad wielkością pozycji i stop-lossem przed tą datą. Data pochodzi z Yahoo Finance i bywa szacunkowa.</div>`;
}

function renderUpcomingEarnings() {
  const body = el("earningsBody");
  if (!body) return;
  const warn = getEarningsWarningDays();
  const horizon = Math.max(30, warn);

  const rows = [];
  state.resultsByTicker.forEach((r) => {
    const e = earningsOf(r);
    if (e && e.days <= horizon) rows.push({ ticker: r.ticker, date: e.date, days: e.days });
  });

  if (rows.length === 0) {
    body.innerHTML = `<p class="empty-state">Brak wyników kwartalnych w ciągu najbliższych ${horizon} dni.</p>`;
    return;
  }

  rows.sort((a, b) => a.days - b.days);
  body.innerHTML = rows.map((row) => {
    const held = state.portfolioTickers.has(row.ticker);
    return `
      <div class="eff-row earnings-row${row.days <= warn ? " earnings-row--soon" : ""}">
        <span class="eff-row__cat">${escapeHtml(row.ticker)}${held ? " 💼" : ""}</span>
        <span class="eff-row__stat">${escapeHtml(row.date)} · ${earningsWhenLabel(row.days)}</span>
      </div>`;
  }).join("") + `<p class="eff-note">💼 = masz tę spółkę w portfelu. Daty z Yahoo Finance bywają szacunkowe - potwierdź w kalendarzu spółki.</p>`;
}

async function refreshHeldTickers() {
  try {
    const res = await fetch("/api/portfolio");
    if (!res.ok) return;
    const positions = await res.json();
    state.portfolioTickers = new Set(positions.map((p) => p.ticker));
    // Też zasila state.lastPortfolio (stop-loss/cel na wykresie, patrz
    // drawPositionPriceLines) - bez tego kliknięcie alertu w logu PRZED
    // pierwszym wejściem w zakładkę Portfel otwierałoby wykres bez linii
    // stopu, czyli akurat w tym jednym, najważniejszym przypadku (świeży
    // alert) niczego by nie pokazało. renderPortfolio i tak nadpisze to
    // świeższymi danymi, gdy użytkownik odwiedzi zakładkę Portfel.
    if (!state.lastPortfolio) state.lastPortfolio = positions;
    renderUpcomingEarnings();
  } catch (err) {
    // panel wyników działa też bez oznaczeń portfela
  }
}

// ---------------- Renderowanie kart wyników ----------------
function renderResultCard(r) {
  const stamp = stampInfo(r.combined.category);
  const trend = r.sentiment_trend ? r.sentiment_trend.trend_direction : "-";
  const fund = r.fundamentals;
  const currency = r.technical.metrics.currency || "USD";
  const target = fund && fund.available ? fund.metrics.analyst_target_mean : null;
  const upside = fund && fund.available ? fund.metrics.analyst_upside_pct : null;
  const targetLabel = target != null
    ? `${target} ${currency}${upside != null ? ` (${upside > 0 ? "+" : ""}${upside}%)` : ""}`
    : "—";

  const age = timeAgo(r.analyzed_at);
  const ageHtml = age
    ? `<span class="data-age ${age.isStale ? "data-age--stale" : ""}" title="Ostatnia analiza tej spółki">🕐 ${age.label}</span>`
    : "";

  const earn = earningsOf(r);
  const earningsHtml = earn ? earningsBadgeHtml(earn.date, earn.days) : "";

  const sparkId = `analysis-spark-${r.ticker.replace(/[^a-zA-Z0-9_-]/g, "_")}`;

  const card = document.createElement("div");
  card.className = "result-card result-card--with-sparkline result-card--customizable";

  card.innerHTML = `
    <div class="stamp stamp--${stamp.cls}" title="${escapeHtml(r.combined.category)}">${stamp.label.replace("\n", "<br>")}</div>

    <div class="result-card__info">
      <div>
        <span class="result-card__ticker">${r.ticker}</span>
        <span class="result-card__name">${r.name}</span>
        ${ageHtml}${earningsHtml}
      </div>
      <div class="result-card__xtb">XTB: ${r.xtb_symbol}</div>
      ${r.discovery_reason ? `<div class="result-card__reason">💡 ${escapeHtml(r.discovery_reason)}</div>` : ""}
    </div>

    <div class="result-card__metric" data-col="target">
      <div class="result-card__metric-value">${targetLabel}</div>
      <div class="result-card__metric-label">Cena docelowa</div>
    </div>

    <div class="result-card__metric" data-col="signal">
      <div class="result-card__metric-value">${r.technical.signal}</div>
      <div class="result-card__metric-label">Sygnał tech.</div>
    </div>

    <div class="result-card__metric" data-col="score">
      <div class="result-card__metric-value">${r.combined.final_score}</div>
      <div class="result-card__metric-label">Wynik / ${trend}</div>
    </div>

    <div class="result-card__sparkline sparkline-cell" data-col="chart">
      <span class="sparkline" id="${sparkId}"></span>
    </div>

    <div class="result-card__category category--${stamp.cls}">
      ${r.combined.category}
    </div>
  `;

  card.addEventListener("click", () => openChart(r.ticker, r.name));

  // Ten sam sparkline co w Portfelu. Pomijamy pobranie, jeśli kolumna jest
  // schowana (personalizacja kolumn) - nie ma sensu ściągać danych do
  // elementu, który i tak zaraz dostanie display:none.
  if (state.columnPrefs.chart !== false) {
    loadSparklineInto(r.ticker, sparkId);
  }

  return card;
}

const _categoryOrder = {
  "WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia": 0,
  "NEUTRALNIE POZYTYWNIE - brak jednoznacznego sygnału wejścia": 1,
  "BRAK SYGNAŁU / POCZEKAJ": 2,
  "UNIKAJ - świeży gwałtowny spadek (sprawdź przyczynę)": 3,
  "UNIKAJ - blisko szczytu trendu": 4,
};

function resultSortValue(r, key) {
  switch (key) {
    case "name": return r.name.toLowerCase();
    case "target": {
      const f = r.fundamentals;
      return f && f.available ? (f.metrics.analyst_upside_pct ?? -Infinity) : -Infinity;
    }
    case "signal": return r.technical.signal;
    case "score": return r.combined.final_score;
    case "category": return _categoryOrder[r.combined.category] ?? 9;
    default: return "";
  }
}

function renderResultsGrid(containerId, results, emptyMessage, sortState) {
  const container = el(containerId);
  container.innerHTML = "";
  // Karty poniżej są tworzone od nowa przy każdym wywołaniu, więc widoczność
  // kolumn (personalizacja) trzeba nałożyć ponownie za każdym razem - tylko
  // dla watchlisty i propozycji AI, portfel ma własną, stałą siatkę.
  const isCustomizable = containerId === "resultsGrid" || containerId === "discoveredGrid";

  if (!results || results.length === 0) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = emptyMessage;
    container.appendChild(p);
    if (isCustomizable) applyColumnVisibility(state.columnPrefs);
    return;
  }

  let sorted;
  if (sortState && sortState.key) {
    sorted = [...results].sort((a, b) => {
      const va = resultSortValue(a, sortState.key);
      const vb = resultSortValue(b, sortState.key);
      const cmp = typeof va === "string" ? va.localeCompare(vb) : va - vb;
      return sortState.dir === "asc" ? cmp : -cmp;
    });
  } else {
    sorted = [...results].sort(
      (a, b) => (_categoryOrder[a.combined.category] ?? 9) - (_categoryOrder[b.combined.category] ?? 9)
    );
  }
  sorted.forEach((r) => container.appendChild(renderResultCard(r)));
  if (isCustomizable) applyColumnVisibility(state.columnPrefs);
}

// ---------------- Heatmapa watchlisty (widok alternatywny do kart) ----------------
state.heatmapMode = localStorage.getItem("heatmapMode") === "1";

// Skala koloru wg zmiany dnia, nasycenie rośnie do +/-5% (dalej już maks. nasycenie) -
// spójne z czerwienią/zielenią używaną gdzie indziej w interfejsie (positive/negative).
function heatmapColor(changePct) {
  if (changePct == null) return "rgba(148, 148, 158, 0.18)";
  const capped = Math.max(-5, Math.min(5, changePct));
  const intensity = Math.abs(capped) / 5;
  return capped >= 0
    ? `rgba(52, 199, 89, ${0.12 + intensity * 0.55})`
    : `rgba(255, 69, 58, ${0.12 + intensity * 0.55})`;
}

function renderHeatmap(results) {
  const wrap = el("watchlistHeatmap");
  wrap.innerHTML = "";
  if (!results || results.length === 0) {
    wrap.innerHTML = `<p class="empty-state">Czekam na pierwszy cykl analizy…</p>`;
    return;
  }
  const sorted = [...results].sort((a, b) => {
    const va = a.technical?.metrics?.day_change_pct ?? -Infinity;
    const vb = b.technical?.metrics?.day_change_pct ?? -Infinity;
    return vb - va;
  });
  sorted.forEach((r) => {
    const change = r.technical?.metrics?.day_change_pct;
    const tile = document.createElement("div");
    tile.className = "heatmap-tile";
    tile.style.background = heatmapColor(change);
    tile.innerHTML = `
      <span class="heatmap-tile__ticker">${escapeHtml(r.ticker)}</span>
      <span class="heatmap-tile__change">${change != null ? (change >= 0 ? "+" : "") + change.toFixed(2) + "%" : "—"}</span>
    `;
    tile.addEventListener("click", () => openChart(r.ticker, r.name));
    wrap.appendChild(tile);
  });
}

function setHeatmapMode(enabled) {
  state.heatmapMode = enabled;
  localStorage.setItem("heatmapMode", enabled ? "1" : "0");
  el("heatmapToggleBtn").classList.toggle("is-active", enabled);
  el("resultsGrid").style.display = enabled ? "none" : "";
  document.querySelector('.results-header[data-sort-group="main"]').style.display = enabled ? "none" : "";
  el("watchlistHeatmap").style.display = enabled ? "" : "none";
  if (enabled) renderHeatmap(state.lastPayload?.results);
}

el("heatmapToggleBtn").addEventListener("click", () => setHeatmapMode(!state.heatmapMode));
setHeatmapMode(state.heatmapMode);

// ---------------- Sortowanie klikalnych nagłówków ----------------
const state_sort = { main: { key: null, dir: "asc" }, discovered: { key: null, dir: "asc" }, portfolio: { key: null, dir: "asc" }, closed: { key: null, dir: "asc" } };

function setupSparklineHeaders() {
  document
    .querySelectorAll(
      '.results-header[data-sort-group="main"], ' +
      '.results-header[data-sort-group="discovered"], ' +
      '.results-header[data-sort-group="portfolio"]'
    )
    .forEach((header) => {
      // Klucz do naprawy rozjazdu: kontener nagłówka MUSI dostać tę samą
      // 7-kolumnową siatkę co karty ze sparkline'em, inaczej dołożona niżej
      // 7. komórka tekstowa i tak wyląduje w siatce 6-kolumnowej.
      header.classList.add("results-header--with-sparkline");
      const isPortfolio = header.dataset.sortGroup === "portfolio";
      if (isPortfolio) header.classList.add("results-header--portfolio");

      if (!header.querySelector(".results-header__sparkline")) {
        const children = Array.from(header.children);
        if (!children.length) return;

        const categoryHeader = children[children.length - 1];
        const chartHeader = document.createElement("span");
        chartHeader.className = "results-header__sparkline";
        chartHeader.textContent = "Wykres";
        chartHeader.dataset.col = "chart";  // używane przez personalizację kolumn (tylko main/discovered)

        header.insertBefore(chartHeader, categoryHeader);
      }

      // Portfel ma dodatkową (8.) kolumnę na przycisk "Analiza AI" - patrz
      // --grid-cols-portfolio w style.css - żeby tekst rekomendacji nie
      // siedział w tej samej komórce co przycisk, tylko czysto pod
      // nagłówkiem "Rekomendacja", tak jak "Kategoria" w watchliście.
      if (isPortfolio && !header.querySelector(".results-header__ai-btn")) {
        const children = Array.from(header.children);
        const categoryHeader = children[children.length - 1];
        const btnHeader = document.createElement("span");
        btnHeader.className = "results-header__ai-btn";
        header.insertBefore(btnHeader, categoryHeader);
      }
    });
}

function setupSortableHeaders() {
  document.querySelectorAll(".results-header[data-sort-group]").forEach((header) => {
    const group = header.dataset.sortGroup;
    header.querySelectorAll("[data-sort-key]").forEach((span) => {
      span.addEventListener("click", () => {
        const key = span.dataset.sortKey;
        const s = state_sort[group];
        if (s.key === key) {
          s.dir = s.dir === "asc" ? "desc" : "asc";
        } else {
          s.key = key;
          s.dir = "asc";
        }
        header.querySelectorAll("[data-sort-key]").forEach((el2) => {
          el2.classList.toggle("is-sorted", el2 === span);
          el2.dataset.sortArrow = s.dir === "asc" ? "▲" : "▼";
        });

        if (group === "main" && state.lastPayload) {
          renderResultsGrid("resultsGrid", state.lastPayload.results, "Czekam na pierwszy cykl analizy…", s);
        } else if (group === "discovered" && state.lastPayload) {
          renderResultsGrid("discoveredGrid", state.lastPayload.discovered_results, "Brak propozycji w tym cyklu.", s);
        } else if (group === "portfolio" && state.lastPortfolio) {
          renderPortfolio(state.lastPortfolio, s);
        } else if (group === "closed") {
          loadClosedPortfolio();
        }
      });
    });
  });
}

function renderMacro(macroContext) {
  const regimeEl = el("riskRegime");
  const vixEl = el("vixValue");
  const tnxEl = el("tnxValue");
  const notesEl = el("macroNotes");

  if (!macroContext || !macroContext.available) {
    regimeEl.textContent = "brak danych";
    regimeEl.className = "macro-readout__value";
    vixEl.textContent = "—";
    tnxEl.textContent = "—";
    notesEl.textContent = "";
    return;
  }

  const regimeClassMap = { SPOKOJNY: "regime-spokojny", PODWYZSZONY: "regime-podwyzszony", WYSOKI: "regime-wysoki" };
  regimeEl.textContent = macroContext.risk_regime;
  regimeEl.className = "macro-readout__value " + (regimeClassMap[macroContext.risk_regime] || "");
  vixEl.textContent = macroContext.vix_last ?? "—";
  tnxEl.textContent = macroContext.treasury_10y_yield_pct != null ? `${macroContext.treasury_10y_yield_pct}%` : "—";
  notesEl.textContent = (macroContext.notes || []).join(" ");
}

const MACRO_EVENT_ICON = { FOMC: "🇺🇸", NBP: "🇵🇱", CPI: "📈" };

function renderMacroCalendar(events) {
  const wrap = el("macroCalendar");
  if (!events || events.length === 0) {
    wrap.innerHTML = "";
    return;
  }
  wrap.innerHTML = `
    <div class="macro-calendar">
      <div class="macro-calendar__title">Nadchodzące wydarzenia</div>
      ${events.map((e) => `
        <div class="macro-calendar__row">
          <span class="macro-calendar__icon">${MACRO_EVENT_ICON[e.kind] || "📅"}</span>
          <span class="macro-calendar__label">${escapeHtml(e.label)}</span>
          <span class="macro-calendar__days">${e.days_away === 0 ? "dziś" : e.days_away === 1 ? "jutro" : `za ${e.days_away} dni`}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---------------- Watchlista ----------------
async function loadWatchlist() {
  const res = await fetch("/api/watchlist");
  const items = await res.json();
  const ul = el("watchlistUl");
  ul.innerHTML = "";
  items.forEach((c) => {
    const li = document.createElement("li");
    li.className = "watchlist__item";
    li.innerHTML = `
      <div>
        <span class="watchlist__ticker">${c.ticker}</span>
        <span class="watchlist__name">${escapeHtml(c.name)}</span>
      </div>
      <button class="watchlist__remove" title="Usuń z watchlisty" data-ticker="${c.ticker}">✕</button>
    `;
    ul.appendChild(li);
  });
  ul.querySelectorAll(".watchlist__remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const ticker = btn.dataset.ticker;
      if (!confirm(`Usunąć ${ticker} z watchlisty?`)) return;
      await fetch(`/api/watchlist/${encodeURIComponent(ticker)}`, { method: "DELETE" });
      await loadWatchlist();
    });
  });
}

el("addForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ticker = el("addTicker").value.trim();
  const name = el("addName").value.trim();
  const xtb = el("addXtb").value.trim();
  if (!ticker) return;

  const res = await fetch("/api/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker, name, xtb_symbol: xtb }),
  });

  if (res.ok) {
    el("addForm").reset();
    await loadWatchlist();
    appendLog({ level: "info", message: `Dodano ${ticker.toUpperCase()} do watchlisty. Pojawi się w kolejnym cyklu analizy.` });
  } else {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Nie udało się dodać spółki.");
  }
});

// ---------------- Wyniki (cache + odświeżanie po WS) ----------------
async function loadResults() {
  const res = await fetch("/api/results");
  const payload = await res.json();
  applyResultsPayload(payload);
}

function applyResultsPayload(payload) {
  state.lastPayload = payload;
  state.resultsByTicker = new Map();
  (payload.results || []).forEach((r) => state.resultsByTicker.set(r.ticker, r));
  (payload.discovered_results || []).forEach((r) => state.resultsByTicker.set(r.ticker, r));

  renderDailyBrief(payload.daily_brief);
  renderMacro(payload.macro_context);
  renderMacroCalendar(payload.macro_calendar);
  renderSectorConcentration(payload.sector_concentration);
  renderUpcomingEarnings();
  renderResultsGrid("resultsGrid", payload.results, "Czekam na pierwszy cykl analizy…", state_sort.main);
  if (state.heatmapMode) renderHeatmap(payload.results);
  renderResultsGrid("discoveredGrid", payload.discovered_results, "Brak propozycji w tym cyklu.", state_sort.discovered);
  el("mainGeneratedAt").textContent = payload.generated_at
    ? `ostatnia aktualizacja: ${formatDateTime(payload.generated_at)}`
    : "brak danych";

  checkForAlerts(payload.results, payload.discovered_results);
}

function renderDailyBrief(brief) {
  const card = el("dailyBriefCard");
  if (!brief) {
    card.style.display = "none";
    return;
  }
  el("dailyBriefText").textContent = brief;
  card.style.display = "";
}

function renderSectorConcentration(list) {
  const body = el("sectorBody");
  if (!body) return;
  if (!list || list.length === 0) {
    body.innerHTML = `<p class="empty-state">Brak koncentracji — sygnały "WARTO OBSERWOWAĆ" są rozproszone sektorowo.</p>`;
    return;
  }
  const rows = list.map((s) => `
    <div class="eff-row">
      <span class="eff-row__cat">${escapeHtml(s.sector)}</span>
      <span class="eff-row__stat">${s.count} spółki: ${escapeHtml(s.tickers.join(", "))}</span>
    </div>`).join("");
  body.innerHTML = rows + `<p class="eff-note">Kilka sygnałów "WARTO OBSERWOWAĆ" z tego samego sektora naraz to często jeden skoncentrowany zakład, a nie kilka niezależnych okazji.</p>`;
}

function formatDateTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return iso;
  }
}

// ---------------- Log na żywo ----------------
function appendLog({ level, message, ts, ticker }) {
  const panel = el("logPanel");
  const line = document.createElement("div");
  const isAlert = message.startsWith("🔔");
  line.className = `log__line log__line--${level || "info"}` + (ticker ? " log__line--clickable" : "");
  const time = ts ? ts.split(" ")[1] || ts : new Date().toLocaleTimeString("pl-PL");
  line.innerHTML = `<span class="log__ts">${time}</span>${escapeHtml(message)}`;
  if (ticker) {
    // Alert bez kontekstu (dlaczego, jaki stop-loss) jest bezużyteczny - klik
    // przenosi wprost na wykres tej spółki, gdzie stop-loss/cel są narysowane
    // jako linie (patrz openChart) i widać pełne uzasadnienie rekomendacji.
    line.title = `Kliknij, żeby zobaczyć ${ticker} na wykresie`;
    line.addEventListener("click", () => openChart(ticker, ticker));
  }
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
  // limit widocznych linii, żeby DOM nie rósł bez końca
  while (panel.children.length > 300) panel.removeChild(panel.firstChild);

  // Odznaka na szufladzie logu - jedyny ślad po alertach cenowych/sygnałowych,
  // odkąd natywne powiadomienia przeglądarki usunięto (nie działały w Brave).
  // Bez tego zwinięta szuflada = alert przechodzi zupełnie niezauważony.
  if (isAlert) {
    const drawer = el("logDrawer");
    if (drawer && drawer.classList.contains("is-collapsed")) {
      state.unseenAlertCount += 1;
      updateLogDrawerBadge();
    }
  }
}

function updateLogDrawerBadge() {
  const badge = el("logDrawerBadge");
  if (!badge) return;
  if (state.unseenAlertCount > 0) {
    badge.textContent = state.unseenAlertCount > 9 ? "9+" : String(state.unseenAlertCount);
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
}

async function loadLogs() {
  const res = await fetch("/api/logs?limit=80");
  const logs = await res.json();
  el("logPanel").innerHTML = "";
  logs.forEach((l) => appendLog(l));
}

// ---------------- Status / next run countdown ----------------
function setConnected(connected, isError = false) {
  const dot = document.querySelector("#connPill .status-pill__dot");
  const label = el("connLabel");
  dot.classList.toggle("is-live", connected && !isError);
  dot.classList.toggle("is-error", isError);
  label.textContent = isError ? "błąd połączenia" : connected ? "live" : "łączę…";
}

function updateNextRunLabel() {
  const labelEl = el("nextRunLabel");
  if (!state.nextRunAt) {
    labelEl.textContent = "—";
    return;
  }
  const diffSec = Math.max(0, Math.round(state.nextRunAt - Date.now() / 1000));
  const m = Math.floor(diffSec / 60);
  const s = diffSec % 60;
  labelEl.textContent = `kolejny cykl za ${m}:${String(s).padStart(2, "0")}`;
}
setInterval(updateNextRunLabel, 1000);

async function loadStatus() {
  const res = await fetch("/api/status");
  const status = await res.json();
  state.nextRunAt = status.next_run_at;
  el("runNowBtn").disabled = status.is_running;
  if (status.is_running) el("runNowBtn").textContent = "Analiza w toku…";
}

el("runNowBtn").addEventListener("click", async () => {
  el("runNowBtn").disabled = true;
  el("runNowBtn").textContent = "Analiza w toku…";
  await fetch("/api/run-now", { method: "POST" });
});

// Sprawdzenie alertów NA ŻĄDANIE - ta sama logika co cicha pętla w tle
// (price_alert_interval_seconds), ale bez czekania na jej najbliższy tick.
// Sam alert (jeśli jakiś przyjdzie) i tak dotrze przez WebSocket jak zwykle
// (case "price_alert" w handleWsMessage) - tu tylko dajemy znać, że w ogóle
// sprawdziliśmy, bo przy zerze trafień WS milczałby i przycisk wyglądałby,
// jakby nic nie zrobił.
const checkAlertsNowBtn = el("checkAlertsNowBtn");
if (checkAlertsNowBtn) {
  checkAlertsNowBtn.addEventListener("click", async () => {
    checkAlertsNowBtn.disabled = true;
    const originalLabel = checkAlertsNowBtn.textContent;
    checkAlertsNowBtn.textContent = "Sprawdzam…";
    try {
      const res = await fetch("/api/portfolio/check-alerts-now", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      if (!data.alerts_sent) {
        appendLog({ level: "info", message: "🔔 Sprawdzono alerty cenowe - żadna pozycja nie przebiła stop-lossu ani celu." });
      }
      // data.alerts_sent > 0 -> same alerty przyjdą osobno przez WebSocket
      // (z tickerem, klikalne) - nie duplikujemy ich tu drugą linią logu.
    } catch (err) {
      appendLog({ level: "error", message: `🔔 Nie udało się sprawdzić alertów: ${String(err)}` });
    } finally {
      checkAlertsNowBtn.disabled = false;
      checkAlertsNowBtn.textContent = originalLabel;
    }
  });
}

// Testowa wiadomość na Discorda - weryfikuje webhook_url z config.yaml bez
// czekania na prawdziwy alert cenowy (osobny endpoint, patrz web_app.py).
const testDiscordBtn = el("testDiscordBtn");
if (testDiscordBtn) {
  testDiscordBtn.addEventListener("click", async () => {
    testDiscordBtn.disabled = true;
    const originalLabel = testDiscordBtn.textContent;
    testDiscordBtn.textContent = "Wysyłam…";
    try {
      const res = await fetch("/api/notifications/test", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      appendLog({ level: "success", message: "🧪 Testowa wiadomość wysłana na Discorda - sprawdź kanał." });
    } catch (err) {
      appendLog({ level: "error", message: `🧪 Test Discorda nie powiódł się: ${String(err)}` });
    } finally {
      testDiscordBtn.disabled = false;
      testDiscordBtn.textContent = originalLabel;
    }
  });
}

// ---------------- WebSocket ----------------
function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    setConnected(true);
    state.wsReconnectDelay = 1500;
  };

  ws.onclose = () => {
    setConnected(false, true);
    setTimeout(connectWebSocket, state.wsReconnectDelay);
    state.wsReconnectDelay = Math.min(state.wsReconnectDelay * 1.5, 15000);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    handleWsMessage(msg);
  };
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case "progress":
      appendLog({ level: msg.level, message: msg.message });
      break;
    case "run_started":
      el("runNowBtn").disabled = true;
      el("runNowBtn").textContent = "Analiza w toku…";
      appendLog({ level: "info", message: "— Rozpoczęto nowy cykl analizy —" });
      break;
    case "results":
      applyResultsPayload(msg.payload);
      break;
    case "run_finished":
      el("runNowBtn").disabled = false;
      el("runNowBtn").textContent = "Odśwież teraz";
      state.nextRunAt = msg.next_run_at;
      appendLog({ level: "success", message: "— Cykl analizy zakończony —" });
      break;
    case "watchlist_changed":
      loadWatchlist();
      break;
    case "ticker_updated":
      applyTickerUpdate(msg.result);
      checkForAlerts([msg.result], []);
      break;
    case "price_alert":
      appendLog({
        level: msg.alert_type === "stop_loss" ? "error" : "success",
        message: `🔔 ${msg.title}: ${msg.body}`,
        ticker: msg.ticker,
      });
      break;
    default:
      break;
  }
}

// ---------------- Wykres (TradingView Lightweight Charts) ----------------
async function openChart(ticker, name) {
  state.openTicker = ticker;
  el("chartModalTitle").textContent = `${name} (${ticker})`;
  el("chartModal").classList.add("is-open");

  const r = state.resultsByTicker.get(ticker);
  el("modalDetails").innerHTML = r
    ? renderDetailsHtml(r)
    : `<p class="empty-state">Brak jeszcze danych analizy dla tej spółki — pojawią się po najbliższym cyklu.</p>`;
  if (r) {
    setupPositionCalculator(r);
    const btn = document.getElementById("manualRefreshBtn");
    if (btn) btn.addEventListener("click", () => refreshTickerLive(btn.dataset.ticker));
  }

  const container = el("chartContainer");
  container.innerHTML = "";
  const positionLegend = el("chartPositionLegend");
  positionLegend.style.display = "none";
  positionLegend.innerHTML = "";

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 420,
    layout: { background: { color: "transparent" }, textColor: "#8A93A8", fontFamily: "IBM Plex Mono, monospace" },
    grid: { vertLines: { color: "#2A3346" }, horzLines: { color: "#2A3346" } },
    timeScale: { borderColor: "#2A3346" },
    rightPriceScale: { borderColor: "#2A3346" },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: "#4F9D69", downColor: "#C1533D",
    borderUpColor: "#6DBE85", borderDownColor: "#DA6A52",
    wickUpColor: "#4F9D69", wickDownColor: "#C1533D",
  });
  const ma50Series = chart.addLineSeries({ color: "#C9A227", lineWidth: 2 });
  // Fiolet, nie czerwień - czerwień jest teraz ZAREZERWOWANA dla stop-lossu
  // (patrz drawPositionPriceLines); wcześniej SMA200 i linia stop-lossu
  // miały DOKŁADNIE ten sam odcień (#DA6A52) i wizualnie się zlewały.
  const ma200Series = chart.addLineSeries({ color: "#8B7FE8", lineWidth: 2 });

  state.chart = chart;

  const loading = el("chartLoading");
  loading.classList.remove("is-hidden");

  try {
    const res = await fetch(`/api/chart/${encodeURIComponent(ticker)}?period=1y`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const cleanCandles = data.candles.filter(
      (c) => c.open != null && c.high != null && c.low != null && c.close != null
    );
    candleSeries.setData(cleanCandles);
    drawPositionPriceLines(candleSeries, ticker);

    const cleanMa50 = data.ma50.filter((p) => p.value != null);
    const cleanMa200 = data.ma200.filter((p) => p.value != null);
    ma50Series.setData(cleanMa50);
    ma200Series.setData(cleanMa200);
    chart.timeScale().fitContent();
    loading.classList.add("is-hidden");

    // Znaczniki historycznych werdyktów narzędzia (WARTO/UNIKAJ/NEUTRALNIE)
    // naniesione na oś czasu wykresu - osobny fetch, żeby jego ewentualny
    // błąd nie psuł już wyrenderowanego wykresu ceny.
    try {
      const candleDates = cleanCandles.map((c) => c.time);
      const histRes = await fetch(`/api/score-history/${encodeURIComponent(ticker)}?limit=400`);
      if (histRes.ok) {
        const history = await histRes.json();
        const markers = buildVerdictMarkers(history, candleDates);
        candleSeries.setMarkers(markers);
      }
    } catch (markerErr) {
      console.warn("Nie udało się nanieść znaczników historii werdyktów:", markerErr);
    }
  } catch (err) {
    loading.classList.add("is-hidden");
    container.innerHTML = `<p class="empty-state">Nie udało się pobrać danych wykresu: ${escapeHtml(String(err))}</p>`;
  }
}

// Poziomy stop-loss/cel/śr. cena zakupu wprost na wykresie - odpowiada na
// "dlaczego ten alert" bez przełączania się między kartą portfela a
// wykresem: jeśli świeca jest pod czerwoną przerywaną linią, to DLATEGO
// poleciał alert stop-lossu. Działa tylko gdy spółka jest w portfelu -
// state.lastPortfolio wypełnia się przy pierwszym wejściu w zakładkę Portfel
// (patrz renderPortfolio), więc otwarcie wykresu z watchlisty PRZED
// odwiedzeniem Portfela po prostu nie narysuje linii (nic się nie psuje).
function drawPositionPriceLines(candleSeries, ticker) {
  const lots = (state.lastPortfolio || []).filter((p) => p.ticker === ticker);
  if (!lots.length) return;

  const bestLot = [...lots].sort(
    (a, b) => (ACTION_PRIORITY[a.action] ?? 9) - (ACTION_PRIORITY[b.action] ?? 9)
  )[0];
  const legendItems = [];

  // Paleta linii pozycji celowo NIE dzieli koloru z żadnym innym elementem
  // wykresu (świece, SMA, strzałki werdyktów) - inaczej, przy kilku liniach
  // naraz, użytkownik nie odróżni "to stop czy to SMA200" na pierwszy rzut
  // oka. LargeDashed (grubszy rytm kreski) zamiast zwykłego Dashed - jedyny
  // styl linii used wyłącznie tutaj, dodatkowe wizualne rozgraniczenie poza
  // samym kolorem.
  const stopLoss = bestLot.custom_stop ?? bestLot.suggested_stop_loss ?? null;
  if (stopLoss != null) {
    const stopLabel = bestLot.custom_stop ? "własny" : "ATR";
    candleSeries.createPriceLine({
      price: stopLoss, color: "#DA6A52", lineWidth: 2, lineStyle: 3,
      axisLabelVisible: true, title: `stop (${stopLabel})`,
    });
    legendItems.push(`<span><i class="legend-swatch legend-swatch--stop"></i> Stop-loss (${stopLabel}): ${fmtMoney(stopLoss, bestLot.currency)}</span>`);
  }
  if (bestLot.custom_target != null) {
    // Cyjan, nie zieleń - zieleń jest już zajęta przez świece wzrostowe I
    // strzałkę "KUP" I akcent SMA - cel dostaje drugi, odrębny akcent marki
    // (ten sam cyjan co gdzie indziej w UI), żeby nie utonął w tej samej
    // zieleni co reszta wykresu.
    candleSeries.createPriceLine({
      price: bestLot.custom_target, color: "#56E1E3", lineWidth: 2, lineStyle: 3,
      axisLabelVisible: true, title: "cel",
    });
    legendItems.push(`<span><i class="legend-swatch legend-swatch--target"></i> Twój cel: ${fmtMoney(bestLot.custom_target, bestLot.currency)}</span>`);
  }

  const totalShares = lots.reduce((s, l) => s + l.shares, 0);
  const totalCost = lots.reduce((s, l) => s + (l.cost_basis ?? l.shares * l.buy_price), 0);
  if (totalShares > 0) {
    const avgBuy = totalCost / totalShares;
    candleSeries.createPriceLine({
      price: avgBuy, color: "#8A93A8", lineWidth: 1, lineStyle: 1,
      axisLabelVisible: true, title: "śr. zakup",
    });
    legendItems.push(`<span><i class="legend-swatch legend-swatch--avgbuy"></i> Śr. cena zakupu: ${fmtMoney(avgBuy.toFixed(2), bestLot.currency)}</span>`);
  }

  if (bestLot.reasons && bestLot.reasons.length) {
    legendItems.push(`<span class="modal__legend-note">— ${escapeHtml(bestLot.reasons.join("; "))}</span>`);
  }

  const positionLegend = el("chartPositionLegend");
  if (legendItems.length) {
    positionLegend.innerHTML = legendItems.join("");
    positionLegend.style.display = "";
  }
}

// ---------------- Znaczniki werdyktów na wykresie ----------------
function verdictMarkerStyle(category) {
  if (category.startsWith("WARTO OBSERWOWAĆ")) {
    return { color: "#6DBE85", shape: "arrowUp", position: "belowBar", text: "KUP" };
  }
  if (category.startsWith("UNIKAJ - świeży")) {
    return { color: "#DA6A52", shape: "arrowDown", position: "aboveBar", text: "SPADEK" };
  }
  if (category.startsWith("UNIKAJ")) {
    return { color: "#DA6A52", shape: "arrowDown", position: "aboveBar", text: "SZCZYT" };
  }
  if (category.startsWith("NEUTRALNIE")) {
    return { color: "#D9A441", shape: "circle", position: "aboveBar", text: "N" };
  }
  // "BRAK SYGNAŁU / POCZEKAJ" celowo pomijamy - to najczęstsza kategoria,
  // pokazywanie jej znacznika za każdym razem zaśmiecałoby wykres bez
  // dodatkowej wartości informacyjnej.
  return null;
}

/**
 * Zamienia historię werdyktów (z dowolnymi znacznikami czasu, łącznie z
 * dniami bez sesji giełdowej - np. analiza uruchomiona w weekend) na
 * znaczniki Lightweight Charts, których "time" MUSI odpowiadać realnej
 * świecy. Każdy wpis jest "przyklejany" do najbliższej wcześniejszej sesji
 * giełdowej, a duplikaty tego samego dnia są redukowane do ostatniego
 * (najbardziej aktualnego) werdyktu z danego dnia.
 */
function buildVerdictMarkers(scoreHistory, candleDates) {
  if (!candleDates || candleDates.length === 0) return [];

  const byDay = new Map(); // snapped date -> marker (nadpisywany -> zostaje ostatni z dnia)

  for (const entry of scoreHistory) {
    const style = verdictMarkerStyle(entry.category);
    if (!style) continue;

    const entryDate = entry.ts.split(" ")[0]; // "YYYY-MM-DD HH:MM:SS" -> "YYYY-MM-DD"
    const snapped = snapToTradingDay(entryDate, candleDates);
    if (!snapped) continue;

    byDay.set(snapped, {
      time: snapped,
      position: style.position,
      color: style.color,
      shape: style.shape,
      text: style.text,
    });
  }

  return Array.from(byDay.values()).sort((a, b) => (a.time < b.time ? -1 : 1));
}

function snapToTradingDay(dateStr, sortedCandleDates) {
  // sortedCandleDates jest posortowane rosnąco (tak zwraca yfinance/pandas)
  let best = null;
  for (const d of sortedCandleDates) {
    if (d <= dateStr) best = d;
    else break;
  }
  return best || sortedCandleDates[0];
}

function closeChart() {
  state.openTicker = null;
  el("chartModal").classList.remove("is-open");
  if (state.chart) {
    state.chart.remove();
    state.chart = null;
  }
}
el("chartModalClose").addEventListener("click", closeChart);
el("chartModalBackdrop").addEventListener("click", closeChart);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeChart(); });

// Dynamiczne skalowanie wykresów przy zmianie rozmiaru okna/panelu.
window.addEventListener("resize", () => {
  const container = el("chartContainer");
  if (state.chart && container) {
    state.chart.applyOptions({ width: container.clientWidth });
  }
  const equityContainer = el("equityChartContainer");
  if (equityChart && equityContainer) {
    equityChart.applyOptions({ width: equityContainer.clientWidth });
  }
  const benchContainer = el("benchChartContainer");
  if (benchChart && benchContainer) {
    benchChart.applyOptions({ width: benchContainer.clientWidth });
  }
});

// ---------------- Zakładki ----------------
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("is-active"));
    btn.classList.add("is-active");
    const tab = btn.dataset.tab;
    el("tab-analysis").style.display = tab === "analysis" ? "" : "none";
    el("tab-portfolio").style.display = tab === "portfolio" ? "" : "none";
    el("tab-closed").style.display = tab === "closed" ? "" : "none";
    if (tab === "portfolio") {
      loadPortfolio();
      loadPortfolioEquitySection();
      loadDividends();
      loadRebalancing();
    }
    if (tab === "closed") { loadClosedPortfolio(); loadTaxSummary(); }
  });
});

// ---------------- Portfel ----------------
async function loadPortfolio() {
  try {
    const res = await fetch("/api/portfolio");
    const positions = await res.json();
    state.portfolioTickers = new Set(positions.map((p) => p.ticker));
    renderUpcomingEarnings();
    renderPortfolio(positions, state_sort.portfolio);
    loadPortfolioRisk();
    loadPortfolioStatistics();
  } catch (err) {
    console.warn("Nie udało się pobrać portfela:", err);
  }
}

// ---------------- Dywidendy ----------------
async function loadDividends() {
  try {
    const res = await fetch("/api/dividends");
    const data = await res.json();
    renderDividendSummary(data.summary);
    renderDividendTable(data.dividends);
  } catch (err) {
    console.warn("Nie udało się pobrać dywidend:", err);
  }
}

function renderDividendSummary(summary) {
  const panel = el("dividendSummaryPanel");
  const years = Object.keys(summary?.by_year || {}).sort().reverse();
  if (years.length === 0) {
    panel.innerHTML = `<p class="empty-state">💵 Brak zarejestrowanych dywidend — dodaj ręcznie po lewej albo zaimportuj raport XTB.</p>`;
    return;
  }

  let html = `<div class="detail-section">
    <h4 class="detail-section__title">💵 Przychód z dywidend (${escapeHtml(summary.base_currency)})</h4>
    <div class="metric-grid">
      <div><span>Łącznie brutto</span><strong class="positive">+${summary.total_gross.toFixed(2)}</strong></div>
      <div><span>Podatek u źródła</span><strong class="negative">-${summary.total_wht.toFixed(2)}</strong></div>
      <div><span>Łącznie netto</span><strong class="positive">+${summary.total_net.toFixed(2)}</strong></div>
    </div>
    <div class="tax-year-grid">`;

  years.forEach((year) => {
    const y = summary.by_year[year];
    html += `
      <div class="tax-year-card">
        <div class="tax-year-card__header"><strong>${year}</strong><span>${y.count} wypłat</span></div>
        <div class="metric-grid">
          <div><span>Brutto</span><strong class="positive">+${y.gross.toFixed(2)}</strong></div>
          <div><span>Podatek u źródła</span><strong class="negative">-${y.wht.toFixed(2)}</strong></div>
          <div><span>Netto</span><strong class="positive">+${y.net.toFixed(2)}</strong></div>
        </div>
      </div>`;
  });
  html += `</div>`;

  if (summary.conversion_notes && summary.conversion_notes.length) {
    html += `<ul class="detail-list">${summary.conversion_notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`;
  }

  html += `<p class="detail-disclaimer">⚠️ To podsumowanie przepływu gotówki, NIE rozliczenie podatkowe. Polski podatek od dywidend zagranicznych to różnica między 19% a podatkiem u źródła już potrąconym za granicą (jeśli stawka źródłowa jest niższa) — narzędzie tej ewentualnej dopłaty nie wylicza. Skonsultuj się z doradcą podatkowym.</p></div>`;
  panel.innerHTML = html;
}

function renderDividendTable(dividends) {
  const wrap = el("dividendTableWrap");
  if (!dividends || dividends.length === 0) {
    wrap.innerHTML = "";
    return;
  }

  let html = `<table class="data-table">
    <thead><tr>
      <th>Data</th><th>Spółka</th><th>Brutto</th><th>Podatek u źródła</th><th>Netto</th><th>Źródło</th><th></th>
    </tr></thead><tbody>`;

  dividends.forEach((d) => {
    const wht = d.withholding_tax || 0;
    const net = d.amount_gross - wht;
    html += `<tr>
      <td>${escapeHtml(d.pay_date)}</td>
      <td>${escapeHtml(d.ticker)}</td>
      <td>${fmtMoney(d.amount_gross, d.currency)}</td>
      <td>${wht ? "-" + fmtMoney(wht, d.currency) : "—"}</td>
      <td>${fmtMoney(net, d.currency)}</td>
      <td title="${escapeHtml(d.notes || "")}">${d.source === "xtb_import" ? "XTB" : "ręczne"}</td>
      <td><button class="watchlist__remove" title="Usuń" data-id="${d.id}">✕</button></td>
    </tr>`;
  });
  html += `</tbody></table>`;
  wrap.innerHTML = html;

  wrap.querySelectorAll(".watchlist__remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Usunąć tę dywidendę?")) return;
      await fetch(`/api/dividends/${btn.dataset.id}`, { method: "DELETE" });
      await loadDividends();
    });
  });
}

el("dividendForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    ticker: el("divTicker").value.trim(),
    currency: el("divCurrency").value.trim(),
    pay_date: el("divPayDate").value,
    amount_gross: parseFloat(el("divAmountGross").value),
    withholding_tax: el("divWithholdingTax").value ? parseFloat(el("divWithholdingTax").value) : null,
    notes: el("divNotes").value.trim(),
  };
  if (!payload.ticker || !payload.currency || !payload.pay_date || isNaN(payload.amount_gross) || payload.amount_gross <= 0) {
    alert("Uzupełnij ticker, walutę, datę i prawidłową kwotę brutto.");
    return;
  }
  const res = await fetch("/api/dividends", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    e.target.reset();
    await loadDividends();
  } else {
    const b = await res.json().catch(() => ({}));
    alert(b.detail || "Nie udało się dodać dywidendy.");
  }
});

// ---------------- Rebalancing (docelowe wagi per ticker) ----------------
async function loadRebalancing() {
  try {
    const res = await fetch("/api/portfolio/rebalancing");
    const data = await res.json();
    renderRebalancing(data);
    renderSectorRebalancing(data.sectors, data.base_currency);
  } catch (err) {
    console.warn("Nie udało się pobrać danych rebalancingu:", err);
  }
}

function renderRebalancing(data) {
  const panel = el("rebalancingPanel");
  if (!data || !data.has_targets) {
    panel.innerHTML = `<p class="empty-state">⚖ Brak ustawionych celów alokacji — dodaj pierwszy w formularzu wyżej (np. „AAPL” → 15%), żeby zobaczyć odchylenie od celu i sugestie kup/sprzedaj.</p>`;
    return;
  }

  const sumWarning = Math.abs(data.target_sum_pct - 100) > 0.5
    ? `<p class="detail-disclaimer">⚠️ Suma celów to ${data.target_sum_pct}%, nie 100% — odchylenia poniżej wciąż są liczone poprawnie względem KAŻDEGO celu z osobna, ale całość nie sumuje się do pełnego portfela.</p>`
    : "";

  let html = `<div class="dividend-table-wrap"><table class="data-table">
    <thead><tr>
      <th>Spółka</th><th>Aktualnie</th><th>Cel</th><th>Odchylenie</th><th>Sugestia</th><th></th>
    </tr></thead><tbody>`;

  data.suggestions.forEach((s) => {
    const driftCls = s.needs_action ? (s.drift_pct > 0 ? "negative" : "positive") : "";
    let action = "—";
    if (s.needs_action && s.suggested_shares != null) {
      action = s.suggested_shares > 0
        ? `kup ~${Math.abs(s.suggested_shares).toFixed(2)} szt. (${fmtMoney(Math.abs(s.diff_value), data.base_currency)})`
        : `sprzedaj ~${Math.abs(s.suggested_shares).toFixed(2)} szt. (${fmtMoney(Math.abs(s.diff_value), data.base_currency)})`;
    } else if (s.needs_action) {
      action = `${s.diff_value >= 0 ? "dokup" : "sprzedaj"} ${fmtMoney(Math.abs(s.diff_value), data.base_currency)} (brak ceny)`;
    }
    html += `<tr>
      <td>${escapeHtml(s.ticker)}${!s.has_position ? ` <span class="ike-badge" title="Brak dziś pozycji w portfelu">brak</span>` : ""}</td>
      <td>${s.current_pct}%</td>
      <td>${s.target_pct}%</td>
      <td class="${driftCls}">${s.drift_pct >= 0 ? "+" : ""}${s.drift_pct} pkt%</td>
      <td>${escapeHtml(action)}</td>
      <td><button class="watchlist__remove" title="Usuń cel" data-ticker="${escapeHtml(s.ticker)}">✕</button></td>
    </tr>`;
  });
  html += `</tbody></table></div>${sumWarning}
    <p class="detail-disclaimer">⚖ Sugestie to proste przeliczenie (wartość docelowa − wartość bieżąca) / cena — nie uwzględniają kosztów transakcyjnych, podatku przy sprzedaży ani minimalnych wielkości zleceń brokera. Próg odchylenia: ${data.tolerance_pct} pkt% (portfolio.rebalance_tolerance_pct w config.yaml).</p>`;

  panel.innerHTML = html;
  panel.querySelectorAll(".watchlist__remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch(`/api/portfolio/targets/${encodeURIComponent(btn.dataset.ticker)}`, { method: "DELETE" });
      await loadRebalancing();
    });
  });
}

el("targetForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ticker = el("targetTicker").value.trim();
  const pct = parseFloat(el("targetPct").value);
  if (!ticker || isNaN(pct) || pct < 0 || pct > 100) {
    alert("Podaj ticker i cel w przedziale 0-100%.");
    return;
  }
  const res = await fetch("/api/portfolio/targets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker, target_weight_pct: pct }),
  });
  if (res.ok) {
    e.target.reset();
    await loadRebalancing();
  } else {
    const b = await res.json().catch(() => ({}));
    alert(b.detail || "Nie udało się ustawić celu.");
  }
});

function renderSectorRebalancing(data, baseCurrency) {
  const panel = el("rebalancingSectorPanel");
  if (!data || !data.has_targets) {
    panel.innerHTML = `<p class="empty-state">⚖ Brak ustawionych celów wg sektora.</p>`;
    return;
  }

  const sumWarning = Math.abs(data.target_sum_pct - 100) > 0.5
    ? `<p class="detail-disclaimer">⚠️ Suma celów to ${data.target_sum_pct}%, nie 100%.</p>`
    : "";

  let html = `<div class="dividend-table-wrap"><table class="data-table">
    <thead><tr>
      <th>Sektor</th><th>Aktualnie</th><th>Cel</th><th>Odchylenie</th><th>Sugestia</th><th></th>
    </tr></thead><tbody>`;

  data.suggestions.forEach((s) => {
    const driftCls = s.needs_action ? (s.drift_pct > 0 ? "negative" : "positive") : "";
    const action = s.needs_action
      ? `${s.diff_value >= 0 ? "dokup" : "sprzedaj"} ~${fmtMoney(Math.abs(s.diff_value), baseCurrency)}`
      : "—";
    html += `<tr>
      <td title="${s.tickers.length ? escapeHtml(s.tickers.join(", ")) : "brak dziś pozycji w tym sektorze"}">${escapeHtml(s.sector)}</td>
      <td>${s.current_pct}%</td>
      <td>${s.target_pct}%</td>
      <td class="${driftCls}">${s.drift_pct >= 0 ? "+" : ""}${s.drift_pct} pkt%</td>
      <td>${escapeHtml(action)}</td>
      <td><button class="watchlist__remove" title="Usuń cel" data-sector="${escapeHtml(s.sector)}">✕</button></td>
    </tr>`;
  });
  html += `</tbody></table></div>${sumWarning}
    <p class="detail-disclaimer">⚖ Sugestia to kwota do dokupienia/sprzedania w SUMIE w tym sektorze, rozłożona na dowolne spółki, które do niego należą — bez wskazania konkretnej spółki ani liczby akcji.</p>`;

  panel.innerHTML = html;
  panel.querySelectorAll(".watchlist__remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch(`/api/portfolio/sector-targets/${encodeURIComponent(btn.dataset.sector)}`, { method: "DELETE" });
      await loadRebalancing();
    });
  });
}

el("sectorTargetForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const sector = el("sectorTargetName").value.trim();
  const pct = parseFloat(el("sectorTargetPct").value);
  if (!sector || isNaN(pct) || pct < 0 || pct > 100) {
    alert("Podaj sektor i cel w przedziale 0-100%.");
    return;
  }
  const res = await fetch("/api/portfolio/sector-targets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sector, target_weight_pct: pct }),
  });
  if (res.ok) {
    e.target.reset();
    await loadRebalancing();
  } else {
    const b = await res.json().catch(() => ({}));
    alert(b.detail || "Nie udało się ustawić celu.");
  }
});

// Współdzielone przez renderPortfolio (wybór "najpilniejszej" transzy na
// kartę grupy) i drawPositionPriceLines (ten sam wybór na wykresie) - było
// zduplikowane wyłącznie w renderPortfolio, wyciągnięte żeby oba miejsca
// zawsze zgadzały się co do tego, która transza jest "reprezentatywna".
const ACTION_PRIORITY = {
  "ROZWAŻ SPRZEDAŻ": 0,
  "SPRAWDŹ PRZYCZYNĘ - NIE DOKUPUJ AUTOMATYCZNIE": 1,
  "ROZWAŻ REALIZACJĘ ZYSKU": 2,
  "TRZYMAJ": 3,
  "BRAK DANYCH": 4,
};

function actionClass(action) {
  if (action === "ROZWAŻ SPRZEDAŻ" || action === "SPRAWDŹ PRZYCZYNĘ - NIE DOKUPUJ AUTOMATYCZNIE") return "avoid";
  if (action === "ROZWAŻ REALIZACJĘ ZYSKU") return "watch";
  if (action === "BRAK DANYCH") return "wait";
  return "buy";
}

// Stop-loss AKTYWNY (własny > ATR) i cel, zawsze widoczne na karcie pozycji -
// wcześniej pokazywało się TYLKO gdy użytkownik ręcznie ustawił własny stop,
// więc domyślny (ATR) stop-loss, mimo że policzony i realnie używany przez
// alerty cenowe, był praktycznie niewidoczny.
function stopTargetLineHtml(stopLoss, stopSource, target, currency) {
  if (stopLoss == null && target == null) return "";
  const parts = [];
  if (stopLoss != null) {
    const tag = stopSource === "own" ? "własny" : "ATR";
    parts.push(`🛑 stop ${fmtMoney(stopLoss, currency)} <span class="stop-source-tag">(${tag})</span>`);
  }
  if (target != null) parts.push(`🎯 cel ${fmtMoney(target, currency)}`);
  return `<div class="result-card__reason result-card__reason--stop">${parts.join(" · ")}</div>`;
}

// Tooltip pieczątki - "dlaczego" ta rekomendacja, na bazie tych samych
// `reasons`, które backend już liczy (report.evaluate_portfolio_position),
// ale wcześniej nigdzie w UI portfela się nie pojawiały.
function actionTooltip(action, reasons) {
  const base = action || "—";
  return reasons && reasons.length ? `${base} — ${reasons.join("; ")}` : base;
}

function portfolioGroupSortValue(group, key) {
  switch (key) {
    case "name": return group.ticker;
    case "price": return group.currentPrice ?? -Infinity;
    case "pl_pct": return group.totalPlPct ?? -Infinity;
    case "pl_value": return group.totalPl;
    case "action": return group.actionPriority;
    default: return "";
  }
}

function renderPortfolio(positions, sortState) {
  state.lastPortfolio = positions;
  const grid = el("portfolioGrid");
  grid.innerHTML = "";

  if (!positions || positions.length === 0) {
    grid.innerHTML = `<p class="empty-state">📭 Brak pozycji — dodaj pierwszą po lewej stronie.</p>`;
    el("sumInvested").textContent = "—";
    el("sumValue").textContent = "—";
    el("sumPl").textContent = "—";
    el("sumPlPct").textContent = "—";
    return;
  }

  const actionPriority = ACTION_PRIORITY;

  const byTicker = new Map();
  positions.forEach((p) => {
    if (!byTicker.has(p.ticker)) byTicker.set(p.ticker, []);
    byTicker.get(p.ticker).push(p);
  });

  const groups = [];
  const byCurrency = new Map(); // waluta -> {cost, value}

  byTicker.forEach((lots, ticker) => {
    const currency = lots.find((l) => l.currency)?.currency || "USD";
    const totalShares = lots.reduce((s, l) => s + l.shares, 0);
    const totalCost = lots.reduce((s, l) => s + (l.cost_basis ?? l.shares * l.buy_price), 0);
    const totalValue = lots.reduce((s, l) => s + (l.market_value ?? 0), 0);
    const avgBuyPrice = totalCost / totalShares;
    const currentPrice = lots.find((l) => l.current_price != null)?.current_price ?? null;
    const totalPl = totalValue - totalCost;
    const totalPlPct = totalCost > 0 ? (totalPl / totalCost * 100) : null;
    const bestLot = [...lots].sort((a, b) => (actionPriority[a.action] ?? 9) - (actionPriority[b.action] ?? 9))[0];

    if (!byCurrency.has(currency)) byCurrency.set(currency, { cost: 0, value: 0 });
    const curSums = byCurrency.get(currency);
    curSums.cost += totalCost;
    curSums.value += totalValue;

    const groupAge = timeAgo(lots.find((l) => l.analyzed_at)?.analyzed_at);
    const ageHtml = groupAge
      ? ` <span class="data-age ${groupAge.isStale ? "data-age--stale" : ""}">🕐 ${groupAge.label}</span>`
      : "";

    const lotWithEarnings = lots.find((l) => daysUntilDate(l.next_earnings_date) != null);
    const earningsHtml = lotWithEarnings
      ? earningsBadgeHtml(lotWithEarnings.next_earnings_date, daysUntilDate(lotWithEarnings.next_earnings_date))
      : "";

    groups.push({
      ticker, lots, currency, totalShares, totalCost, totalValue, avgBuyPrice, currentPrice,
      totalPl, totalPlPct, action: bestLot.action, actionPriority: actionPriority[bestLot.action] ?? 9,
      ageHtml: ageHtml + earningsHtml,
      // Stop/cel/uzasadnienie NAJPILNIEJSZEJ transzy (ta sama, z której
      // wzięto `action`) - reprezentatywne dla karty grupy; szczegóły per
      // transza nadal widoczne po rozwinięciu (renderLotCard).
      stopLoss: bestLot.custom_stop ?? bestLot.suggested_stop_loss ?? null,
      stopSource: bestLot.custom_stop ? "own" : "atr",
      target: bestLot.custom_target ?? null,
      reasons: bestLot.reasons || [],
    });
  });

  let sortedGroups = groups;
  if (sortState && sortState.key) {
    sortedGroups = [...groups].sort((a, b) => {
      const va = portfolioGroupSortValue(a, sortState.key);
      const vb = portfolioGroupSortValue(b, sortState.key);
      const cmp = typeof va === "string" ? va.localeCompare(vb) : va - vb;
      return sortState.dir === "asc" ? cmp : -cmp;
    });
  }

  sortedGroups.forEach((g) => grid.appendChild(renderPortfolioGroupCard(g)));

  renderCurrencySummaryBar(byCurrency);
}

function renderCurrencySummaryBar(byCurrency) {
  const bar = el("portfolioSummaryBar");
  bar.innerHTML = "";

  byCurrency.forEach((sums, currency) => {
    const pl = sums.value - sums.cost;
    const plPct = sums.cost > 0 ? (pl / sums.cost * 100) : 0;
    const wrap = document.createElement("div");
    wrap.className = "portfolio-summary-currency";
    wrap.innerHTML = `
      <div class="portfolio-summary-currency__label">Podsumowanie — ${escapeHtml(currency)}</div>
      <div class="portfolio-summary-bar">
        <div class="portfolio-summary-bar__item"><span>Zainwestowano</span><strong>${sums.cost.toFixed(2)}</strong></div>
        <div class="portfolio-summary-bar__item"><span>Wartość bieżąca</span><strong>${sums.value.toFixed(2)}</strong></div>
        <div class="portfolio-summary-bar__item"><span>Zysk / strata</span><strong class="${pl >= 0 ? "positive" : "negative"}">${pl >= 0 ? "+" : ""}${pl.toFixed(2)}</strong></div>
        <div class="portfolio-summary-bar__item"><span>Zysk / strata %</span><strong class="${plPct >= 0 ? "positive" : "negative"}">${plPct >= 0 ? "+" : ""}${plPct.toFixed(1)}%</strong></div>
      </div>
    `;
    bar.appendChild(wrap);
  });
}

function renderPortfolioGroupCard(g) {
  const cls = actionClass(g.action);
  const groupWrap = document.createElement("div");
  groupWrap.className = "portfolio-group";

  const header = document.createElement("div");
  header.className = "result-card portfolio-card portfolio-group__header result-card--with-sparkline result-card--portfolio-group";
  header.innerHTML = `
    <div class="stamp stamp--${cls}" title="${escapeHtml(actionTooltip(g.action, g.reasons))}">${escapeHtml(g.action).split(" ").slice(0, 2).join("<br>")}</div>
    <div class="result-card__info">
      <div>
        <span class="result-card__ticker">${g.ticker}</span>${g.lots.every((l) => l.account_type === "ike") ? ` <span class="ike-badge" title="Konto IKE - podatek zależy od wieku przy wypłacie, patrz zakładka Zamknięte transakcje">IKE</span>` : ""}
        <span class="result-card__name">${g.lots.length} ${g.lots.length === 1 ? "pozycja" : "pozycje/i"} • śr. ${fmtMoney(g.avgBuyPrice.toFixed(2), g.currency)}</span>
      </div>
      <div class="result-card__xtb">Łącznie ${g.totalShares} szt.${g.ageHtml || ""}<span class="portfolio-group__toggle">▾ rozwiń</span></div>
      ${stopTargetLineHtml(g.stopLoss, g.stopSource, g.target, g.currency)}
    </div>
    <div class="result-card__metric">
      <div class="result-card__metric-value">${fmtMoney(g.currentPrice, g.currency)}</div>
      <div class="result-card__metric-label">Cena bieżąca</div>
    </div>
    <div class="result-card__metric">
      <div class="result-card__metric-value">${g.totalPlPct != null ? (g.totalPlPct >= 0 ? "+" : "") + g.totalPlPct.toFixed(1) + "%" : "—"}</div>
      <div class="result-card__metric-label">Zysk/strata</div>
    </div>
    <div class="result-card__metric">
      <div class="result-card__metric-value">${fmtMoney(g.totalPl.toFixed(2), g.currency)}</div>
      <div class="result-card__metric-label">Wartość P/L</div>
    </div>

    <div class="result-card__sparkline sparkline-cell">
      <span class="sparkline" id="spark-${g.ticker.replace(/[^a-zA-Z0-9_-]/g, "_")}"></span>
    </div>

    <div class="result-card__ai-btn-cell">
      <button class="portfolio-group__ai-btn" title="Zobacz pełną analizę techniczną, sentyment i fundamenty">🔍 Analiza AI</button>
    </div>

    <div class="result-card__category category--${cls}">${escapeHtml(g.action)}</div>
  `;
  header.querySelector(".portfolio-group__toggle").addEventListener("click", (e) => {
    e.stopPropagation();
    groupWrap.classList.toggle("is-expanded");
  });
  header.querySelector(".portfolio-group__ai-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    openChart(g.ticker, g.ticker);
  });
  header.addEventListener("click", () => groupWrap.classList.toggle("is-expanded"));

  const lotsWrap = document.createElement("div");
  lotsWrap.className = "portfolio-group__lots";
  g.lots.forEach((p) => lotsWrap.appendChild(renderLotCard(p)));

  groupWrap.appendChild(header);
  groupWrap.appendChild(lotsWrap);
  loadSparkline(g.ticker);
  return groupWrap;
}

function buildSparklineSvg(closes) {
  // Wewnętrzna rozdzielczość SVG (viewBox) jest teraz większa i NIEZALEŻNA
  // od rzeczywistego rozmiaru na ekranie - width="100%"/height="100%" +
  // preserveAspectRatio="none" rozciąga wykres dokładnie na tyle, ile daje
  // mu kolumna CSS (patrz .sparkline-cell .sparkline svg), więc jest w pełni
  // skalowalny i responsywny, zamiast sztywnych 68x22 pikseli.
  const w = 120, h = 34, pad = 3;
  const min = Math.min(...closes), max = Math.max(...closes);
  const range = max - min || 1;
  const stepX = (w - pad * 2) / (closes.length - 1);
  const points = closes.map((v, i) => {
    const x = pad + i * stepX;
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const color = closes[closes.length - 1] >= closes[0] ? "#6DBE85" : "#DA6A52";
  return `<svg width="100%" height="100%" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

async function loadSparklineInto(ticker, targetId) {
  try {
    const res = await fetch(
      `/api/portfolio/sparkline/${encodeURIComponent(ticker)}`
    );

    if (!res.ok) return;

    const data = await res.json();

    if (!data.closes || data.closes.length < 2) return;

    const target = document.getElementById(targetId);

    if (target) {
      target.innerHTML = buildSparklineSvg(data.closes);
    }
  } catch (err) {
    // Sparkline jest tylko dodatkiem wizualnym.
    // Błąd wykresu nie powinien blokować reszty interfejsu.
  }
}

async function loadSparkline(ticker) {
  await loadSparklineInto(
    ticker,
    `spark-${ticker.replace(/[^a-zA-Z0-9_-]/g, "_")}`
  );
}

function renderLotCard(p) {
  const cls = actionClass(p.action);
  const pctSign = p.unrealized_pct >= 0 ? "+" : "";
  const card = document.createElement("div");
  card.className = "result-card portfolio-card lot-card";
  card.innerHTML = `
    <div class="stamp stamp--${cls}" title="${escapeHtml(actionTooltip(p.action, p.reasons))}">${escapeHtml((p.action || "—")).split(" ")[0]}</div>
    <div class="result-card__info">
      <div>
        <span class="result-card__ticker">${p.shares} szt.</span>
        <span class="result-card__name">@ ${fmtMoney(p.buy_price, p.currency)}</span>
      </div>
      <div class="result-card__xtb">Kupione: ${p.buy_date} (${p.horizon || "—"})${p.account_type === "ike" ? ` <span class="ike-badge" title="Konto IKE - podatek zależy od wieku przy wypłacie, patrz zakładka Zamknięte transakcje">IKE</span>` : ""}</div>
      ${stopTargetLineHtml(p.custom_stop ?? p.suggested_stop_loss ?? null, p.custom_stop ? "own" : "atr", p.custom_target ?? null, p.currency)}
      ${p.notes ? `<div class="result-card__reason">📝 ${escapeHtml(p.notes)}</div>` : ""}
    </div>
    <div class="result-card__metric">
      <div class="result-card__metric-value">${p.unrealized_pct != null ? pctSign + p.unrealized_pct + "%" : "—"}</div>
      <div class="result-card__metric-label">Zysk/strata</div>
    </div>
    <div class="result-card__metric">
      <div class="result-card__metric-value">${fmtMoney(p.unrealized_value, p.currency)}</div>
      <div class="result-card__metric-label">Wartość P/L</div>
    </div>
    <div class="lot-actions">
      <button class="lot-sell" title="Sprzedaj" data-id="${p.id}">💰</button>
      <button class="lot-edit" title="Edytuj" data-id="${p.id}">✎</button>
      <button class="lot-duplicate" title="Kopiuj jako nową pozycję" data-id="${p.id}">⧉</button>
      <button class="lot-delete" title="Usuń trwale" data-id="${p.id}">✕</button>
    </div>
  `;
  card.dataset.position = JSON.stringify(p);

  card.querySelector(".lot-sell").addEventListener("click", async (e) => {
    e.stopPropagation();
    const sellPrice = prompt(`Cena sprzedaży dla ${p.ticker} (${p.shares} szt. @ ${p.buy_price} ${p.currency || ""})?`);
    if (sellPrice === null || sellPrice.trim() === "") return;
    const parsed = parseFloat(sellPrice);
    if (isNaN(parsed) || parsed <= 0) {
      alert("Nieprawidłowa cena.");
      return;
    }
    const fxInput = prompt(
      `Własny kurs wymiany przy sprzedaży (opcjonalnie, np. rzeczywisty kurs XTB z marżą)?\n` +
      `Zostaw puste, żeby użyć bieżącego kursu rynkowego.`
    );
    let sellFxRate = null;
    if (fxInput !== null && fxInput.trim() !== "") {
      const parsedFx = parseFloat(fxInput);
      if (isNaN(parsedFx) || parsedFx <= 0) {
        alert("Nieprawidłowy kurs wymiany - zignorowano, użyty zostanie bieżący kurs rynkowy.");
      } else {
        sellFxRate = parsedFx;
      }
    }
    const sellDate = new Date().toISOString().slice(0, 10);
    const res = await fetch(`/api/portfolio/${p.id}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sell_price: parsed, sell_date: sellDate, sell_fx_rate: sellFxRate }),
    });
    if (res.ok) {
      await loadPortfolio();
    } else {
      const b = await res.json().catch(() => ({}));
      alert(b.detail || "Nie udało się zamknąć pozycji.");
    }
  });
  // Przycisk ✎ wcześniej nie miał żadnego handlera - startEditPosition() istniało, ale nic go nie wywoływało.
  card.querySelector(".lot-edit").addEventListener("click", (e) => {
    e.stopPropagation();
    startEditPosition(p);
  });
  card.querySelector(".lot-duplicate").addEventListener("click", (e) => {
    e.stopPropagation();
    duplicatePosition(p);
  });
  card.querySelector(".lot-delete").addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm("Usunąć tę pozycję z portfela?")) return;
    await fetch(`/api/portfolio/${p.id}`, { method: "DELETE" });
    await loadPortfolio();
  });

  return card;
}

function startEditPosition(p) {
  el("posEditId").value = p.id;
  el("posTicker").value = p.ticker;
  el("posShares").value = p.shares;
  el("posBuyPrice").value = p.buy_price;
  el("posBuyDate").value = p.buy_date;
  el("posNotes").value = p.notes || "";
  el("posBuyFxRate").value = p.buy_fx_rate ?? "";
  el("posCustomStop").value = p.custom_stop ?? "";
  el("posCustomTarget").value = p.custom_target ?? "";
  el("posAccountIke").checked = p.account_type === "ike";
  el("posSubmitBtn").textContent = "Zapisz zmiany";
  el("posCancelEditBtn").style.display = "";
  el("posTicker").scrollIntoView({ behavior: "smooth", block: "center" });
}

function duplicatePosition(p) {
  el("posEditId").value = "";
  el("posTicker").value = p.ticker;
  el("posShares").value = p.shares;
  el("posBuyPrice").value = p.buy_price;
  el("posBuyDate").value = new Date().toISOString().slice(0, 10);
  el("posNotes").value = p.notes || "";
  // Kurs wymiany celowo NIE jest kopiowany - to nowa transakcja z dzisiejszą
  // datą, więc stary kurs z poprzedniego zakupu by tu nie pasował.
  el("posBuyFxRate").value = "";
  el("posCustomStop").value = p.custom_stop ?? "";
  el("posCustomTarget").value = p.custom_target ?? "";
  el("posAccountIke").checked = p.account_type === "ike";
  el("posSubmitBtn").textContent = "+ Dodaj pozycję";
  el("posCancelEditBtn").style.display = "none";
  el("posTicker").scrollIntoView({ behavior: "smooth", block: "center" });
}

function parseOptionalNumber(raw) {
  const n = parseFloat(raw);
  return isNaN(n) ? null : n;
}

function resetPortfolioForm() {
  el("portfolioForm").reset();
  el("posEditId").value = "";
  el("posSubmitBtn").textContent = "+ Dodaj pozycję";
  el("posCancelEditBtn").style.display = "none";
}

el("posCancelEditBtn").addEventListener("click", resetPortfolioForm);

el("portfolioForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    ticker: el("posTicker").value.trim(),
    shares: parseFloat(el("posShares").value),
    buy_price: parseFloat(el("posBuyPrice").value),
    buy_date: el("posBuyDate").value,
    notes: el("posNotes").value.trim(),
    buy_fx_rate: parseOptionalNumber(el("posBuyFxRate").value),
    custom_stop: parseOptionalNumber(el("posCustomStop").value),
    custom_target: parseOptionalNumber(el("posCustomTarget").value),
    account_type: el("posAccountIke").checked ? "ike" : "standard",
  };
  if (!body.ticker || !body.shares || !body.buy_price || !body.buy_date) return;

  const editId = el("posEditId").value;
  const url = editId ? `/api/portfolio/${editId}` : "/api/portfolio";
  const method = editId ? "PUT" : "POST";

  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    resetPortfolioForm();
    await loadPortfolio();
  } else {
    const b = await res.json().catch(() => ({}));
    alert(b.detail || "Nie udało się zapisać pozycji.");
  }
});


function checkForAlerts(results, discovered) {
  const all = [...(results || []), ...(discovered || [])];
  const prev = state.categoryByTicker;
  const next = new Map();

  all.forEach((r) => {
    const cat = r.combined.category;
    next.set(r.ticker, cat);
    const before = prev.get(r.ticker);
    if (before === cat) return;

    const isHeld = state.portfolioTickers.has(r.ticker);
    if (isHeld && (r.technical.signal === "AT_TOP" || r.technical.signal === "SHARP_DECLINE")) {
      appendLog({
        level: "error",
        message: `🔔 ${r.ticker}: ${r.technical.signal === "AT_TOP" ? "blisko szczytu trendu" : "gwałtowny spadek"} — sprawdź zakładkę Portfel.`,
        ticker: r.ticker,
      });
    } else if (!isHeld && before !== undefined && cat.startsWith("WARTO OBSERWOWAĆ")) {
      appendLog({ level: "success", message: `🔔 ${r.ticker}: nowy sygnał WARTO OBSERWOWAĆ.`, ticker: r.ticker });
    }
  });

  state.categoryByTicker = next;
}

// ---------------- Panel szczegółów spółki ----------------
function fmtPct(v) { return v == null ? "—" : `${v}%`; }

function timeAgo(isoString) {
  if (!isoString) return null;
  const then = new Date(isoString).getTime();
  if (isNaN(then)) return null;
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));

  let label;
  if (diffSec < 60) label = "przed chwilą";
  else if (diffSec < 3600) label = `${Math.floor(diffSec / 60)} min temu`;
  else if (diffSec < 86400) label = `${Math.floor(diffSec / 3600)} godz. temu`;
  else label = `${Math.floor(diffSec / 86400)} dni temu`;

  // Próg ostrzegawczy: dane starsze niż 2h sugerują, że coś mogło nie
  // zadziałać w automatycznym cyklu (domyślnie co 60 min) - warto to
  // wizualnie odróżnić, nie tylko podać liczbę.
  const isStale = diffSec > 2 * 3600;
  return { label, isStale };
}
function fmtMoney(v, currency) {
  if (v == null) return "—";
  return `${v} ${currency || ""}`.trim();
}

function renderDetailsHtml(r) {
  const t = r.technical, s = r.sentiment, c = r.combined;
  const trend = r.sentiment_trend, fund = r.fundamentals;
  const news = r.news_headlines || [];
  const catClass = stampInfo(c.category).cls;

  let html = `
    <div class="detail-section detail-refresh-row">
      <button class="btn btn--secondary" id="manualRefreshBtn" data-ticker="${escapeHtml(r.ticker)}">
        🔄 Odśwież tę spółkę (ceny, sentyment, newsy)
      </button>
      <span id="liveRefreshIndicator" class="live-refresh-indicator is-hidden"></span>
    </div>
    <div class="detail-section detail-summary">
      <span class="detail-summary__badge category--${catClass}">${escapeHtml(c.category)}</span>
      <span class="detail-summary__score">Wynik łączny: <strong>${c.final_score}</strong> (próg: ${c.effective_threshold})</span>
      ${c.hard_blocked ? `<span class="detail-summary__blocked">🚫 Zablokowane twardo (blisko szczytu)</span>` : ""}
    </div>
    ${earningsBannerHtml(r)}`;

  html += `
    <div class="detail-section">
      <h4 class="detail-section__title">Analiza techniczna — ${escapeHtml(t.signal)} (score ${t.score})</h4>
      <ul class="detail-list">${t.reasons.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
        <div class="metric-grid">
          <div><span>RSI</span><strong>${t.metrics.rsi ?? "—"}</strong></div>
          <div><span>Trend (SMA)</span><strong>${t.metrics.uptrend ? "wzrostowy" : "brak"}</strong></div>
          <div><span>Do 52-tyg. maks.</span><strong>${fmtPct(t.metrics.dist_from_52w_high_pct)}</strong></div>
          <div><span>Wolumen (anomalia)</span><strong>${t.metrics.volume_spike ? "tak" : "nie"}</strong></div>
          ${t.metrics.relative_strength ? `<div><span>Siła wzgl. vs ${escapeHtml(t.metrics.relative_strength.benchmark)}</span><strong>${t.metrics.relative_strength.relative_strength_pct >= 0 ? "+" : ""}${t.metrics.relative_strength.relative_strength_pct} pkt%</strong></div>` : ""}
        </div>
    </div>`;

  html += `
    <div class="detail-section">
      <h4 class="detail-section__title">Sentyment newsów (${escapeHtml(s.method)}) — wynik ${s.score}</h4>
      <p class="detail-text">${escapeHtml(s.summary)}</p>
    </div>`;

  if (news.length) {
    html += `
      <div class="detail-section">
        <h4 class="detail-section__title">Aktualne nagłówki</h4>
        <ul class="news-list">${news.map((h) => `
          <li class="news-item">
            ${h.link ? `<a href="${h.link}" target="_blank" rel="noopener">${escapeHtml(h.title)}</a>` : escapeHtml(h.title)}
            <span class="news-item__source">${escapeHtml(h.source || "")}</span>
          </li>`).join("")}
        </ul>
      </div>`;
  }

  if (trend && trend.monthly && trend.monthly.length) {
    html += `
      <div class="detail-section">
        <h4 class="detail-section__title">Trend sentymentu (12 mies.) — ${escapeHtml(trend.trend_direction)}</h4>
        <p class="detail-text">${escapeHtml(trend.llm_narrative)}</p>
      </div>`;
  }

  if (fund && fund.available) {
    html += `
      <div class="detail-section">
        <h4 class="detail-section__title">Fundamenty (wynik ${fund.score})</h4>
        <div class="metric-grid">
          <div><span>P/E</span><strong>${fund.metrics.trailing_pe ?? "—"}</strong></div>
          <div><span>Wzrost przychodów</span><strong>${fmtPct(fund.metrics.revenue_growth_pct)}</strong></div>
          <div><span>Marża netto</span><strong>${fmtPct(fund.metrics.profit_margins_pct)}</strong></div>
          <div><span>Dług/kapitał</span><strong>${fund.metrics.debt_to_equity ?? "—"}</strong></div>
          <div><span>Cena docelowa (śr.)</span><strong>${fmtMoney(fund.metrics.analyst_target_mean, t.metrics.currency)}</strong></div>
          <div><span>Potencjał wg analityków</span><strong>${fmtPct(fund.metrics.analyst_upside_pct)}</strong></div>
          <div><span>Następne wyniki</span><strong>${fund.metrics.next_earnings_date ? escapeHtml(fund.metrics.next_earnings_date) : "—"}</strong></div>
        </div>
        <ul class="detail-list">${fund.flags.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>
      </div>`;
  } else if (fund) {
    html += `<div class="detail-section"><p class="detail-text detail-text--muted">Dane fundamentalne niedostępne dla tej spółki.</p></div>`;
  }

  if (t.metrics.last_price != null && t.metrics.atr != null) {
    html += `
      <div class="detail-section">
        <h4 class="detail-section__title">Kalkulator wielkości pozycji</h4>
        <div class="metric-grid">
          <div><span>Cena bieżąca</span><strong>${fmtMoney(t.metrics.last_price.toFixed(2), t.metrics.currency)}</strong></div>
          <div><span>ATR (14)</span><strong>${fmtMoney(t.metrics.atr.toFixed(2), t.metrics.currency)}</strong></div>
          <div><span>Sugerowany stop-loss</span><strong>${fmtMoney(t.metrics.suggested_stop_loss.toFixed(2), t.metrics.currency)}</strong></div>
          <div><span>Odległość stopu</span><strong>${fmtPct(t.metrics.stop_distance_pct)}</strong></div>
        </div>
        <div class="calc-inputs">
          <label>Kapitał (${escapeHtml(t.metrics.currency || "USD")})
            <input type="number" id="calcCapital" min="0" step="100">
          </label>
          <label>Ryzyko na transakcję (%)
            <input type="number" id="calcRisk" min="0.1" max="10" step="0.1">
          </label>
        </div>
        <div class="calc-result" id="calcResult"></div>
        <p class="detail-disclaimer">Stop-loss = cena − 2×ATR(14). Kapitał wpisuj w walucie notowania spółki (${escapeHtml(t.metrics.currency || "USD")}). To punkt wyjścia do własnego zarządzania ryzykiem, nie gotowa rekomendacja.</p>
      </div>`;
  } else {
    html += `
      <div class="detail-section">
        <h4 class="detail-section__title">Kalkulator wielkości pozycji</h4>
        <p class="detail-text detail-text--muted">Za mało danych historycznych, by policzyć ATR i sugerowany stop-loss.</p>
      </div>`;
  }

  if (r.discovery_reason) {
    html += `
      <div class="detail-section">
        <h4 class="detail-section__title">💡 Dlaczego zaproponowane przez AI</h4>
        <p class="detail-text">${escapeHtml(r.discovery_reason)}</p>
      </div>`;
  }

  html += `<p class="detail-disclaimer">To analiza narzędziowa, nie porada inwestycyjna — zweryfikuj samodzielnie przed decyzją.</p>`;
  return html;
}

// ---------------- Kalkulator wielkości pozycji - logika interaktywna ----------------
function setupPositionCalculator(r) {
  const capitalInput = el("calcCapital");
  const riskInput = el("calcRisk");
  const resultEl = el("calcResult");
  if (!capitalInput || !riskInput || !resultEl) return;

  const lastPrice = r.technical.metrics.last_price;
  const stopLoss = r.technical.metrics.suggested_stop_loss;

  const saved = JSON.parse(localStorage.getItem("posCalcSettings") || "{}");
  capitalInput.value = saved.capital ?? 10000;
  riskInput.value = saved.riskPct ?? 1;

  function recalc() {
    const capital = parseFloat(capitalInput.value) || 0;
    const riskPct = parseFloat(riskInput.value) || 0;
    localStorage.setItem("posCalcSettings", JSON.stringify({ capital, riskPct }));

    const riskAmount = capital * (riskPct / 100);
    const riskPerShare = lastPrice - stopLoss;
    if (riskPerShare <= 0) {
      resultEl.innerHTML = `<p class="detail-text detail-text--muted">Nieprawidłowe dane (stop-loss ≥ cena bieżąca).</p>`;
      return;
    }
    const shares = Math.floor(riskAmount / riskPerShare);
    const positionValue = shares * lastPrice;

    const currency = r.technical.metrics.currency || "USD";
    resultEl.innerHTML = `
      <div class="calc-highlight"><span>Ryzykujesz</span><strong>${riskAmount.toFixed(2)} ${currency}</strong></div>
      <div class="calc-highlight"><span>Sugerowana liczba akcji</span><strong>${shares}</strong></div>
      <div class="calc-highlight"><span>Wartość pozycji</span><strong>${positionValue.toFixed(2)} ${currency}</strong></div>
    `;
  }

  capitalInput.addEventListener("input", recalc);
  riskInput.addEventListener("input", recalc);
  recalc();
}

// ---------------- Panel skuteczności narzędzia ----------------
async function loadEffectiveness() {
  try {
    const res = await fetch("/api/effectiveness");
    const stats = await res.json();
    renderEffectiveness(stats);
  } catch (err) {
    console.warn("Nie udało się pobrać statystyk skuteczności:", err);
  }
}

function renderEffectiveness(stats) {
  const body = el("effectivenessBody");
  const categories = Object.keys(stats || {});
  if (categories.length === 0) {
    body.innerHTML = `<p class="empty-state">Zbieram dane historyczne — wróć za kilka dni.</p>`;
    return;
  }

  const rows = categories.map((cat) => {
    const s = stats[cat];
    const cls = stampInfo(cat).cls;
    const sign = s.avg_return_pct >= 0 ? "+" : "";
    return `
      <div class="eff-row">
        <span class="eff-row__cat category--${cls}">${escapeHtml(cat)}</span>
        <span class="eff-row__stat">${s.count} sygn.</span>
        <span class="eff-row__stat">win rate ${s.win_rate_pct}%</span>
        <span class="eff-row__stat eff-row__return">${sign}${s.avg_return_pct}% śr.</span>
      </div>`;
  }).join("");

  body.innerHTML = rows + `<p class="eff-note">Zmiana ceny od danego werdyktu do najnowszej znanej ceny (min. ${14} dni odstępu). Orientacyjna miara, nie pełny backtest.</p>`;
}

// ---------------- Odświeżanie na żywo pojedynczej spółki ----------------
async function refreshTickerLive(ticker) {
  const indicator = el("liveRefreshIndicator");
  const btn = document.getElementById("manualRefreshBtn");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Odświeżam…"; }
  if (indicator) {
    indicator.textContent = "Odświeżam dane na żywo (ceny, sentyment, newsy)…";
    indicator.classList.remove("is-hidden");
  }
  try {
    const res = await fetch(`/api/refresh/${encodeURIComponent(ticker)}`, { method: "POST" });
    if (res.status === 429) {
      const body = await res.json().catch(() => ({}));
      if (indicator) indicator.textContent = body.detail || "Poczekaj przed kolejnym odświeżeniem.";
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const result = await res.json();
    applyTickerUpdate(result);
    if (indicator) indicator.textContent = `Zaktualizowano na żywo: ${new Date().toLocaleTimeString("pl-PL")}`;
  } catch (err) {
    if (indicator) indicator.textContent = "Nie udało się odświeżyć na żywo — pokazuję ostatnie znane dane.";
    console.warn("Live refresh error:", err);
  } finally {
    const freshBtn = document.getElementById("manualRefreshBtn");
    if (freshBtn) { freshBtn.disabled = false; freshBtn.textContent = "🔄 Odśwież tę spółkę (ceny, sentyment, newsy)"; }
  }
}

function applyTickerUpdate(result) {
  state.resultsByTicker.set(result.ticker, result);
  renderUpcomingEarnings();

  if (state.lastPayload) {
    for (const key of ["results", "discovered_results"]) {
      const arr = state.lastPayload[key] || [];
      const idx = arr.findIndex((r) => r.ticker === result.ticker);
      if (idx !== -1) arr[idx] = result;
    }
    renderResultsGrid("resultsGrid", state.lastPayload.results, "Czekam na pierwszy cykl analizy…", state_sort.main);
    renderResultsGrid("discoveredGrid", state.lastPayload.discovered_results, "Brak propozycji w tym cyklu.", state_sort.discovered);
  }

  // Jeśli modal jest właśnie otwarty dla tej samej spółki - podmień panel na żywo.
  if (state.openTicker === result.ticker) {
    el("modalDetails").innerHTML = renderDetailsHtml(result);
    setupPositionCalculator(result);
    const btn = document.getElementById("manualRefreshBtn");
    if (btn) btn.addEventListener("click", () => refreshTickerLive(btn.dataset.ticker));
  }
}

// ---------------- Zamknięte transakcje ----------------

async function loadClosedPortfolio() {
  try {
    const res = await fetch("/api/portfolio/closed");
    const data = await res.json();
    renderClosedPortfolio(data.positions, data.summary);
  } catch (err) {
    console.warn("Nie udało się pobrać zamkniętych transakcji:", err);
  }
}

function renderClosedSummaryBar(summary) {
  const bar = el("closedSummaryBar");
  bar.innerHTML = "";
  const currencies = Object.keys(summary || {});
  if (currencies.length === 0) return;

  currencies.forEach((currency) => {
    const s = summary[currency];
    const wrap = document.createElement("div");
    wrap.className = "portfolio-summary-currency";
    wrap.innerHTML = `
      <div class="portfolio-summary-currency__label">Zrealizowane — ${escapeHtml(currency)}</div>
      <div class="portfolio-summary-bar">
        <div class="portfolio-summary-bar__item"><span>Liczba transakcji</span><strong>${s.count}</strong></div>
        <div class="portfolio-summary-bar__item"><span>Win rate</span><strong>${s.win_rate_pct}%</strong></div>
        <div class="portfolio-summary-bar__item"><span>Zrealizowany zysk/strata</span>
          <strong class="${s.realized_pl >= 0 ? "positive" : "negative"}">${s.realized_pl >= 0 ? "+" : ""}${s.realized_pl.toFixed(2)}</strong>
        </div>
      </div>
    `;
    bar.appendChild(wrap);
  });
}

function renderClosedPortfolio(positions, summary) {
  renderClosedSummaryBar(summary);
  const grid = el("closedGrid");
  grid.innerHTML = "";

  if (!positions || positions.length === 0) {
    grid.innerHTML = `<p class="empty-state">🗂 Brak zamkniętych transakcji.</p>`;
    return;
  }

  let sorted = positions;
  if (state_sort.closed.key) {
    sorted = [...positions].sort((a, b) => {
      let va, vb;
      switch (state_sort.closed.key) {
        case "name": va = a.ticker; vb = b.ticker; break;
        case "buy": va = a.buy_price; vb = b.buy_price; break;
        case "sell": va = a.sell_price; vb = b.sell_price; break;
        case "pl_pct": va = a.realized_pl_pct; vb = b.realized_pl_pct; break;
        default: va = vb = 0;
      }
      const cmp = typeof va === "string" ? va.localeCompare(vb) : va - vb;
      return state_sort.closed.dir === "asc" ? cmp : -cmp;
    });
  }

  sorted.forEach((p) => {
    const cls = p.realized_pl >= 0 ? "buy" : "avoid";
    const card = document.createElement("div");
    card.className = "result-card portfolio-card closed-card";
    card.innerHTML = `
      <div class="stamp stamp--${cls}">${p.realized_pl >= 0 ? "▲<br>ZYSK" : "▼<br>STRATA"}</div>
      <div class="result-card__info">
        <div>
          <span class="result-card__ticker">${p.ticker}</span>${p.account_type === "ike" ? ` <span class="ike-badge" title="Konto IKE - podatek zależy od wieku przy wypłacie, patrz podsumowanie podatkowe niżej">IKE</span>` : ""}
          <span class="result-card__name">${p.shares} szt. (${p.holding_days ?? "?"} dni)</span>
        </div>
        ${p.notes ? `<div class="result-card__reason">📝 ${escapeHtml(p.notes)}</div>` : ""}
      </div>
      <div class="result-card__metric">
        <div class="result-card__metric-value">${fmtMoney(p.buy_price, p.currency)}</div>
        <div class="result-card__metric-label">${p.buy_date}</div>
      </div>
      <div class="result-card__metric">
        <div class="result-card__metric-value">${fmtMoney(p.sell_price, p.currency)}</div>
        <div class="result-card__metric-label">${p.sell_date}</div>
      </div>
      <div class="result-card__metric result-card__metric--headline">
        <div class="result-card__metric-value ${p.realized_pl_pct >= 0 ? "positive" : "negative"}">${p.realized_pl_pct >= 0 ? "+" : ""}${p.realized_pl_pct}%</div>
        <div class="result-card__metric-label">${fmtMoney(p.realized_pl, p.currency)}</div>
      </div>
      <div class="result-card__category category--${cls}">
        <button class="watchlist__remove" title="Cofnij sprzedaż" data-id="${p.id}">↺</button>
      </div>
    `;
    card.querySelector(".watchlist__remove").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Cofnąć sprzedaż ${p.ticker}? Pozycja wróci do aktywnego portfela.`)) return;
      await fetch(`/api/portfolio/${p.id}/reopen`, { method: "POST" });
      await loadClosedPortfolio();
    });
    grid.appendChild(card);
  });
}

// ---------------- Panel ryzyka portfela ----------------
async function loadPortfolioRisk() {
  try {
    const res = await fetch("/api/portfolio/risk");
    const data = await res.json();
    renderCombinedSummary(data.combined);
    renderPortfolioRisk(data);
  } catch (err) {
    console.warn("Nie udało się pobrać ryzyka portfela:", err);
  }
}

function renderCombinedSummary(combined) {
  const bar = el("combinedSummaryBar");
  if (!combined || combined.value == null) {
    bar.innerHTML = "";
    return;
  }
  const pl = combined.value - combined.cost;
  const plCls = pl >= 0 ? "positive" : "negative";
  const riskLine = combined.risk_pct != null
    ? `<div class="portfolio-summary-bar__item"><span>Ryzyko (stop-lossy)</span><strong>${combined.risk_amount.toFixed(2)} (${combined.risk_pct}%)</strong></div>`
    : "";
  const skippedNote = (combined.skipped_currencies && combined.skipped_currencies.length)
    ? `<div class="combined-summary__note">Nie udało się przeliczyć kursu dla: ${combined.skipped_currencies.join(", ")} - pominięte w sumie łącznej.</div>`
    : "";

  bar.innerHTML = `
    <div class="combined-summary">
      <div class="combined-summary__label">Podsumowanie łączne — ${escapeHtml(combined.base_currency)}</div>
      <div class="combined-summary__grid">
        <div class="portfolio-summary-bar__item"><span>Zainwestowano</span><strong>${combined.cost.toFixed(2)}</strong></div>
        <div class="portfolio-summary-bar__item"><span>Wartość bieżąca</span><strong>${combined.value.toFixed(2)}</strong></div>
        <div class="portfolio-summary-bar__item"><span>Zysk / strata</span><strong class="${plCls}">${pl >= 0 ? "+" : ""}${pl.toFixed(2)}</strong></div>
        <div class="portfolio-summary-bar__item"><span>Zysk / strata %</span><strong class="${combined.pl_pct >= 0 ? "positive" : "negative"}">${combined.pl_pct != null ? (combined.pl_pct >= 0 ? "+" : "") + combined.pl_pct + "%" : "—"}</strong></div>
        ${riskLine}
      </div>
      ${skippedNote}
    </div>
  `;
}

function renderPortfolioRisk(data) {
  const panel = el("portfolioRiskPanel");
  const riskByCurrency = (data.risk && data.risk.by_currency) || {};
  const currencies = Object.keys(riskByCurrency);

  let html = "";
  if (currencies.length === 0) {
    html += `<p class="empty-state">⚠ Brak otwartych pozycji do oceny ryzyka.</p>`;
  } else {
    html += `<div class="risk-cards">`;
    currencies.forEach((currency) => {
      const r = riskByCurrency[currency];
      html += `
        <div class="risk-card">
          <div class="risk-card__label">Ryzyko przy stop-lossach (${escapeHtml(currency)})</div>
          <div class="risk-card__value">${r.risk_amount.toFixed(2)} ${escapeHtml(currency)}</div>
          <div class="risk-card__sub">${r.risk_pct != null ? r.risk_pct + "% wartości portfela" : "brak danych %"}</div>
        </div>`;
    });
    html += `</div>`;
    if (data.risk.positions_without_stop_loss > 0) {
      html += `<p class="detail-disclaimer">${data.risk.positions_without_stop_loss} pozycji pominięto (brak jeszcze danych stop-loss — poczekaj na analizę).</p>`;
    }
  }

  const exposure = data.sector_exposure || [];
  if (exposure.length > 0) {
    html += `<div class="detail-section"><h4 class="detail-section__title">Ekspozycja sektorowa portfela</h4>`;
    html += exposure.map((s) => `
      <div class="eff-row eff-row--sector">
        <div class="eff-row__top">
          <span class="eff-row__cat">${escapeHtml(s.sector)}</span>
          <span class="eff-row__stat">${s.pct_of_portfolio}% · ${escapeHtml(s.tickers.join(", "))}</span>
        </div>
        <div class="sector-bar-track"><div class="sector-bar-fill" style="width:${Math.min(100, s.pct_of_portfolio)}%"></div></div>
      </div>`).join("");
    html += `</div>`;
  }

  panel.innerHTML = html;
}

// ---------------- Krzywa kapitału portfela ----------------
let equityChart = null;
let equityCurrentCurrency = null;
let equityViewMode = "value"; // "value" (wartość/koszt) | "twr" (skumulowany zwrot, oczyszczony z wpłat/wypłat)
let equityLastData = null; // dane ostatniej odpowiedzi - przełącznik widoku nie robi ponownego fetcha

async function loadPortfolioEquitySection() {
  try {
    const res = await fetch("/api/portfolio/equity-currencies");
    const currencies = await res.json();
    const options = ["COMBINED", ...currencies]; // "Łącznie" zawsze pierwsze
    equityCurrentCurrency = (equityCurrentCurrency && options.includes(equityCurrentCurrency))
      ? equityCurrentCurrency : "COMBINED";
    renderEquityCurrencySwitcher(options);
    renderEquityViewSwitcher();
    await loadPortfolioEquity(equityCurrentCurrency);
  } catch (err) {
    console.warn("Nie udało się pobrać walut krzywej kapitału:", err);
  }
}

function renderEquityCurrencySwitcher(options) {
  const wrap = el("equityCurrencySwitcher");
  if (!wrap) return;
  wrap.innerHTML = "";
  options.forEach((c) => {
    const btn = document.createElement("button");
    btn.className = "currency-switch-btn" + (c === equityCurrentCurrency ? " is-active" : "");
    btn.textContent = c === "COMBINED" ? "Łącznie" : c;
    btn.addEventListener("click", async () => {
      equityCurrentCurrency = c;
      wrap.querySelectorAll(".currency-switch-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      await loadPortfolioEquity(c);
    });
    wrap.appendChild(btn);
  });
}

function renderEquityViewSwitcher() {
  const wrap = el("equityViewSwitcher");
  if (!wrap) return;
  wrap.innerHTML = "";
  [["value", "Wartość"], ["twr", "Zwrot (TWR)"]].forEach(([mode, label]) => {
    const btn = document.createElement("button");
    btn.className = "currency-switch-btn" + (mode === equityViewMode ? " is-active" : "");
    btn.textContent = label;
    btn.title = mode === "twr"
      ? "Prawdziwy zwrot z inwestycji - dokupienie/sprzedaż pozycji nie zniekształca wyniku (metoda Modified Dietz, złożona geometrycznie)"
      : "Surowa wartość i koszt portfela w czasie";
    btn.addEventListener("click", () => {
      equityViewMode = mode;
      wrap.querySelectorAll(".currency-switch-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      renderEquityChart();
    });
    wrap.appendChild(btn);
  });
}

async function loadPortfolioEquity(currency) {
  const endpoint = currency === "COMBINED" ? "/api/portfolio/equity/combined" : `/api/portfolio/equity/${encodeURIComponent(currency)}`;
  const res = await fetch(endpoint);
  if (!res.ok) return;
  equityLastData = await res.json();
  renderEquityChart();
}

function renderEquityChart() {
  const data = equityLastData;
  const container = el("equityChartContainer");
  const emptyState = el("equityEmptyState");
  const note = el("equityViewNote");
  if (!data) return;

  const usingTwr = equityViewMode === "twr";
  const points = usingTwr ? (data.twr_points || []) : (data.points || []);

  if (!points || points.length < 2) {
    emptyState.textContent = usingTwr
      ? "📉 Za mało danych do policzenia prawdziwego zwrotu (TWR) - potrzeba co najmniej dwóch zdjęć krzywej kapitału z różnych dni."
      : "📉 Za mało danych historycznych — krzywa pojawi się po kilku cyklach analizy.";
    emptyState.style.display = "";
    container.innerHTML = "";
    el("equityDrawdownLabel").textContent = "";
    note.textContent = "";
    return;
  }
  emptyState.style.display = "none";
  container.innerHTML = "";
  note.textContent = usingTwr
    ? "Skumulowany, prawdziwy zwrot z inwestycji (start = 100) - w odróżnieniu od wykresu \"Wartość\", dokupienie lub sprzedaż pozycji NIE podbija ani nie zaniża tej liczby. Orientacyjne - przepływy w walutach obcych przeliczane są przybliżonym kursem rynkowym z dnia transakcji, nie oficjalnym kursem NBP."
    : "";

  if (equityChart) {
    equityChart.remove();
    equityChart = null;
  }

  equityChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 260,
    layout: { background: { color: "transparent" }, textColor: "#8A93A8", fontFamily: "IBM Plex Mono, monospace" },
    grid: { vertLines: { color: "#2A3346" }, horzLines: { color: "#2A3346" } },
    timeScale: { borderColor: "#2A3346" },
    rightPriceScale: { borderColor: "#2A3346" },
  });

  const toTime = (ts) => ts.split(" ")[0];
  const seen = new Set();

  if (usingTwr) {
    const twrSeries = equityChart.addLineSeries({ color: "#C9A227", lineWidth: 2 });
    const twrPoints = [];
    points.forEach((p) => {
      const t = toTime(p.ts);
      if (seen.has(t)) return;
      seen.add(t);
      twrPoints.push({ time: t, value: p.index });
    });
    twrSeries.setData(twrPoints);
    const first = twrPoints[0].value, last = twrPoints[twrPoints.length - 1].value;
    const totalReturn = ((last / first - 1) * 100).toFixed(1);
    el("equityDrawdownLabel").textContent = `skumulowany zwrot: ${totalReturn >= 0 ? "+" : ""}${totalReturn}%`;
  } else {
    const valueSeries = equityChart.addLineSeries({ color: "#C9A227", lineWidth: 2 });
    const costSeries = equityChart.addLineSeries({ color: "#8A93A8", lineWidth: 1, lineStyle: 2 });
    const valuePoints = [];
    const costPoints = [];
    points.forEach((p) => {
      const t = toTime(p.ts);
      if (seen.has(t)) return; // Lightweight Charts wymaga unikalnych, rosnących dat
      seen.add(t);
      valuePoints.push({ time: t, value: p.total_value });
      costPoints.push({ time: t, value: p.total_cost });
    });
    valueSeries.setData(valuePoints);
    costSeries.setData(costPoints);

    const dd = data.drawdown;
    if (dd && dd.available) {
      el("equityDrawdownLabel").textContent =
        `maks. obsunięcie: -${dd.max_drawdown_pct}% (${dd.max_drawdown_peak_date.split(" ")[0]} → ${dd.max_drawdown_trough_date.split(" ")[0]})` +
        (dd.current_drawdown_pct > 0 ? `, bieżące: -${dd.current_drawdown_pct}%` : "");
    } else {
      el("equityDrawdownLabel").textContent = "";
    }
  }

  equityChart.timeScale().fitContent();
}

// ---------------- Orientacyjne podsumowanie podatkowe ----------------
async function loadTaxSummary() {
  try {
    const res = await fetch("/api/portfolio/tax-summary");
    const data = await res.json();
    renderTaxSummary(data);
  } catch (err) {
    console.warn("Nie udało się pobrać podsumowania podatkowego:", err);
  }
}

function taxYearCardsHtml(byYear, extraField) {
  const years = Object.keys(byYear || {}).sort().reverse();
  let html = `<div class="tax-year-grid">`;
  years.forEach((year) => {
    const y = byYear[year];
    html += `
      <div class="tax-year-card">
        <div class="tax-year-card__header"><strong>${year}</strong><span>${y.trade_count} transakcji</span></div>
        <div class="metric-grid">
          <div><span>Zyski</span><strong class="positive">+${y.total_gains.toFixed(2)}</strong></div>
          <div><span>Straty</span><strong class="negative">-${y.total_losses.toFixed(2)}</strong></div>
          <div><span>Wynik netto</span><strong class="${y.net_result >= 0 ? "positive" : "negative"}">${y.net_result >= 0 ? "+" : ""}${y.net_result.toFixed(2)}</strong></div>
          <div>${extraField ? extraField(y) : `<span>Szac. podatek (19%)</span><strong>${y.estimated_tax_19pct.toFixed(2)}</strong>`}</div>
        </div>
      </div>`;
  });
  html += `</div>`;
  return html;
}

function renderTaxSummary(data) {
  const panel = el("taxSummaryPanel");
  const hasStandard = Object.keys(data.by_year || {}).length > 0;
  const hasIke = Object.keys(data.by_year_ike || {}).length > 0;
  if (!hasStandard && !hasIke) {
    panel.innerHTML = "";
    return;
  }

  let html = "";

  if (hasStandard) {
    html += `<div class="detail-section">
      <h4 class="detail-section__title">📊 Orientacyjne podsumowanie podatkowe (${escapeHtml(data.base_currency)})</h4>
      ${taxYearCardsHtml(data.by_year)}
    </div>`;
  }

  if (hasIke) {
    html += `<div class="detail-section">
      <h4 class="detail-section__title">🏛 Konto IKE (${escapeHtml(data.base_currency)})</h4>
      ${taxYearCardsHtml(data.by_year_ike, (y) => `<span>Podatek (2 scenariusze)</span><strong>0 lub ${y.estimated_tax_19pct.toFixed(2)}</strong>`)}
      <p class="detail-disclaimer">🏛 IKE jest zwolnione z podatku Belki TYLKO gdy wypłata następuje po osiągnięciu wieku emerytalnego (lub spełnieniu innych warunków ustawowych) — wtedy podatek wynosi 0. Wcześniejsza wypłata (zwrot) jest opodatkowana DOKŁADNIE tak samo jak konto standardowe (19% od wyniku netto, w kolumnie „Podatek” druga wartość). Narzędzie nie zna Twojego wieku ani okoliczności wypłaty — sam oceń, który scenariusz Cię dotyczy.</p>`;
  }

  if (data.conversion_notes && data.conversion_notes.length) {
    html += `<ul class="detail-list">${data.conversion_notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`;
  }

  const disclaimer = data.nbp_compliant
    ? `⚠️ To orientacyjne wyliczenie pomocnicze, nie zastępuje samodzielnego rozliczenia PIT-38. Kursy walut obcych zostały przeliczone OFICJALNYM średnim kursem NBP z dnia poprzedzającego transakcję (zgodnie z art. 11a ustawy o PIT). Mimo to zawsze zweryfikuj kwoty przed złożeniem deklaracji i skonsultuj się z doradcą podatkowym.`
    : `⚠️ To orientacyjne wyliczenie, NIE oficjalne rozliczenie podatkowe. Część transakcji przeliczono PRZYBLIŻONYM kursem rynkowym (NBP był niedostępny dla części dat/walut) - patrz uwagi wyżej. Przed złożeniem deklaracji zweryfikuj dokładne kwoty w tabelach kursów NBP i skonsultuj się z doradcą podatkowym.`;
  html += `<p class="detail-disclaimer">${disclaimer}</p>`;

  panel.innerHTML = html;
}

// ---------------- Szufladka logu (zwijana, zapamiętuje stan) ----------------
const logDrawer = el("logDrawer");
const logDrawerToggle = el("logDrawerToggle");

function setLogDrawerCollapsed(collapsed) {
  logDrawer.classList.toggle("is-collapsed", collapsed);
  localStorage.setItem("logDrawerCollapsed", collapsed ? "1" : "0");
  if (!collapsed) {
    state.unseenAlertCount = 0;
    updateLogDrawerBadge();
  }
}

logDrawerToggle.addEventListener("click", () => {
  setLogDrawerCollapsed(!logDrawer.classList.contains("is-collapsed"));
});

setLogDrawerCollapsed(localStorage.getItem("logDrawerCollapsed") === "1");

// ---------------- Czat z asystentem (lokalny LLM) ----------------
state.chatHistory = [];

el("chatToggleBtn").addEventListener("click", () => {
  el("chatPanel").classList.toggle("is-open");
});
el("chatCloseBtn").addEventListener("click", () => el("chatPanel").classList.remove("is-open"));

function appendChatMessage(role, text) {
  const wrap = el("chatMessages");
  const div = document.createElement("div");
  div.className = `chat-msg chat-msg--${role}`;
  div.textContent = text;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
  return div;
}

el("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = el("chatInput");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  appendChatMessage("user", question);
  state.chatHistory.push({ role: "user", content: question });

  const thinking = appendChatMessage("assistant", "…");
  thinking.classList.add("chat-msg--thinking");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: state.chatHistory }),
    });
    const data = await res.json();
    thinking.remove();
    appendChatMessage("assistant", data.reply);
    state.chatHistory.push({ role: "assistant", content: data.reply });
  } catch (err) {
    thinking.remove();
    appendChatMessage("assistant", "Błąd połączenia z asystentem — sprawdź, czy serwer działa.");
  }
});

// ---------------- Import raportu XTB ----------------
el("xtbImportBtn").addEventListener("click", async () => {
  const fileInput = el("xtbImportFile");
  const file = fileInput.files[0];
  if (!file) {
    alert("Wybierz plik .xlsx z eksportu XTB.");
    return;
  }

  const btn = el("xtbImportBtn");
  btn.disabled = true;
  btn.textContent = "Importuję…";

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/api/portfolio/import-xtb", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      alert(data.detail || "Nie udało się zaimportować pliku.");
      return;
    }
    let msg = `Zaimportowano: ${data.imported_open} otwartych, ${data.imported_closed} zamkniętych pozycji.`;
    if (data.skipped_duplicates) msg += ` Pominięto ${data.skipped_duplicates} już zaimportowanych wcześniej.`;
    if (data.fx_rates_backfilled) msg += ` Uzupełniono rzeczywisty kurs wymiany dla ${data.fx_rates_backfilled} wcześniej zaimportowanych pozycji.`;
    if (data.closed_from_reimport) msg += ` Zamknięto ${data.closed_from_reimport} pozycji sprzedanych u brokera od poprzedniego importu.`;
    if (data.imported_dividends) msg += ` Zaimportowano ${data.imported_dividends} dywidend.`;
    appendLog({ level: "success", message: `📥 ${msg}` });
    (data.warnings || []).forEach((w) => appendLog({ level: "warning", message: `📥 ${w}` }));
    alert(msg + (data.warnings?.length ? `\n\nUwagi (patrz też log na żywo):\n${data.warnings.slice(0, 5).join("\n")}` : ""));
    fileInput.value = "";
    await loadPortfolio();
    await loadDividends();
    await loadClosedPortfolio();
  } catch (err) {
    alert("Błąd połączenia podczas importu.");
  } finally {
    btn.disabled = false;
    btn.textContent = "📥 Importuj pozycje";
  }
});

el("xtbImportFile").addEventListener("change", () => {
  const file = el("xtbImportFile").files[0];
  el("xtbFileNameLabel").textContent = file ? `✅ ${file.name}` : "📂 Wybierz plik .xlsx z eksportu XTB";
});

// ---------------- Korelacja i zmienność portfela ----------------
async function loadPortfolioStatistics() {
  const panel = el("portfolioStatsPanel");
  if (!panel) return;
  panel.innerHTML = `<p class="empty-state">Liczę korelacje i zmienność (pobieram rok notowań)…</p>`;
  try {
    const res = await fetch("/api/portfolio/statistics");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderPortfolioStatistics(await res.json());
  } catch (err) {
    panel.innerHTML = `<p class="empty-state">Nie udało się policzyć statystyk portfela.</p>`;
    console.warn("Statystyki portfela:", err);
  }
}

// Kolor komórki macierzy: dodatnia korelacja -> bursztyn (im wyższa, tym mocniej), ujemna -> zieleń.
function corrCellStyle(v) {
  if (v == null) return "";
  const alpha = Math.min(0.65, Math.abs(v) * 0.65).toFixed(2);
  return v >= 0 ? `background: rgba(217,164,65,${alpha})` : `background: rgba(79,157,105,${alpha})`;
}

let benchChart = null;

// Sekcja "Portfel vs benchmark" - HTML (wykres rysowany osobno po wstawieniu do DOM).
function benchmarkSectionHtml(bc) {
  if (!bc) return "";
  if (!bc.available) {
    return `<div class="detail-section"><h4 class="detail-section__title">Portfel a benchmark</h4>
      <p class="detail-text detail-text--muted">${escapeHtml(bc.reason || "Porównanie z benchmarkiem niedostępne.")}</p></div>`;
  }
  const sign = (v) => (v >= 0 ? "+" : "");
  const excessCls = bc.excess_return_pct >= 0 ? "positive" : "negative";
  const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
  return `
    <div class="detail-section">
      <h4 class="detail-section__title">Portfel a benchmark (${escapeHtml(bc.benchmark)})</h4>
      <div class="metric-grid stats-grid">
        <div title="Zwrot hipotetycznego portfela o obecnych wagach w badanym okresie"><span>Portfel (hipotet.)</span><strong>${sign(bc.portfolio_return_pct)}${bc.portfolio_return_pct}%</strong></div>
        <div><span>${escapeHtml(bc.benchmark)}</span><strong>${sign(bc.benchmark_return_pct)}${bc.benchmark_return_pct}%</strong></div>
        <div title="Różnica zwrotów w punktach procentowych"><span>Przewaga / strata</span><strong class="${excessCls}">${sign(bc.excess_return_pct)}${bc.excess_return_pct} pkt</strong></div>
        <div title="Jak mocno portfel reaguje na ruchy rynku. 1.0 = tak samo jak benchmark"><span>Beta</span><strong>${bc.beta}</strong></div>
        <div title="Część wyniku niewyjaśniona samą ekspozycją na rynek (w skali roku)"><span>Alfa (rocznie)</span><strong class="${bc.alpha_annual_pct >= 0 ? "positive" : "negative"}">${sign(bc.alpha_annual_pct)}${bc.alpha_annual_pct}%</strong></div>
        <div title="Korelacja dziennych zmian portfela i benchmarku"><span>Korelacja</span><strong>${bc.correlation}</strong></div>
        <div title="Jaką część ruchu rynku portfel łapał w dni wzrostowe"><span>Wychwyt wzrostów</span><strong>${pct(bc.up_capture)}</strong></div>
        <div title="Jaką część ruchu rynku portfel łapał w dni spadkowe (mniej = lepiej)"><span>Wychwyt spadków</span><strong>${pct(bc.down_capture)}</strong></div>
      </div>
      <div class="chart-frame"><div id="benchChartContainer" class="bench-chart-container"></div></div>
      <div class="modal__legend"><span><i class="legend-swatch legend-swatch--ma50"></i> Portfel (start = 100)</span><span><i class="legend-swatch legend-swatch--bench"></i> ${escapeHtml(bc.benchmark)} (start = 100)</span></div>
      <ul class="detail-list">${(bc.insights || []).map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>
    </div>`;
}

function drawBenchmarkChart(bc) {
  if (benchChart) { benchChart.remove(); benchChart = null; }
  const container = el("benchChartContainer");
  if (!container || !bc || !bc.available || !bc.curve || typeof LightweightCharts === "undefined") return;

  benchChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 220,
    layout: { background: { color: "transparent" }, textColor: "#8A93A8", fontFamily: "IBM Plex Mono, monospace" },
    grid: { vertLines: { color: "#2A3346" }, horzLines: { color: "#2A3346" } },
    timeScale: { borderColor: "#2A3346" },
    rightPriceScale: { borderColor: "#2A3346" },
  });
  const portfolioSeries = benchChart.addLineSeries({ color: "#C9A227", lineWidth: 2 });
  const benchSeries = benchChart.addLineSeries({ color: "#8A93A8", lineWidth: 2, lineStyle: 2 });
  const { dates, portfolio, benchmark } = bc.curve;
  portfolioSeries.setData(dates.map((t, i) => ({ time: t, value: portfolio[i] })));
  benchSeries.setData(dates.map((t, i) => ({ time: t, value: benchmark[i] })));
  benchChart.timeScale().fitContent();
}

function renderPortfolioStatistics(data) {
  const panel = el("portfolioStatsPanel");
  const meta = el("portfolioStatsMeta");
  if (benchChart) { benchChart.remove(); benchChart = null; }  // panel jest renderowany od nowa

  if (!data || !data.available) {
    meta.textContent = "";
    panel.innerHTML = `<p class="empty-state">∿ ${escapeHtml((data && data.reason) || "Brak danych do policzenia statystyk.")}</p>`;
    return;
  }

  meta.textContent = `${data.period_start} → ${data.period_end} · ${data.observations} sesji`;

  const sharpeCls = data.sharpe == null ? "" : (data.sharpe >= 1 ? "positive" : (data.sharpe < 0 ? "negative" : ""));
  let html = "";

  (data.warnings || []).forEach((w) => {
    html += `<div class="detail-section warning-banner">⚠️ ${escapeHtml(w)}</div>`;
  });

  html += `
    <div class="metric-grid stats-grid">
      <div title="Odchylenie standardowe dziennych zwrotów przeliczone na rok"><span>Zmienność roczna</span><strong>${data.annual_volatility_pct}%</strong></div>
      <div title="(zwrot roczny - stopa wolna od ryzyka) / zmienność"><span>Sharpe</span><strong class="${sharpeCls}">${data.sharpe ?? "—"}</strong></div>
      <div title="Największy spadek od szczytu w badanym okresie, przy obecnych wagach"><span>Maks. obsunięcie</span><strong class="negative">-${data.max_drawdown_pct}%</strong></div>
      <div title="Średnia korelacja dziennych zwrotów między parami pozycji"><span>Śr. korelacja</span><strong>${data.avg_correlation ?? "—"}</strong></div>
      <div title="Średnia ważona zmienności pozycji / zmienność portfela. 1.0 = brak korzyści z dywersyfikacji"><span>Wsp. dywersyfikacji</span><strong>${data.diversification_ratio ?? "—"}</strong></div>
      <div title="Historyczny zwrot hipotetycznego portfela o obecnych wagach - NIE Twój faktyczny wynik"><span>Zwrot hist. (roczny)</span><strong>${data.annual_return_pct >= 0 ? "+" : ""}${data.annual_return_pct}%</strong></div>
    </div>`;

  html += benchmarkSectionHtml(data.benchmark_comparison);

  const hasPairs = data.top_pairs && data.top_pairs.length;
  const m = data.matrix;
  const hasMatrix = m && m.tickers.length >= 2 && m.tickers.length <= 12;

  if (hasPairs || hasMatrix) {
    html += `<div class="stats-tile-row">`;
    if (hasPairs) {
      html += `<div class="detail-section"><h4 class="detail-section__title">Najsilniej skorelowane pary</h4>`;
      html += data.top_pairs.map((p) => `
        <div class="eff-row">
          <span class="eff-row__cat">${escapeHtml(p.a)} / ${escapeHtml(p.b)}</span>
          <span class="eff-row__stat">${p.correlation}</span>
        </div>`).join("");
      html += `</div>`;
    }
    if (hasMatrix) {
      html += `<div class="detail-section"><h4 class="detail-section__title">Macierz korelacji</h4><div class="corr-wrap"><table class="corr-table"><thead><tr><th></th>`;
      html += m.tickers.map((t) => `<th>${escapeHtml(t)}</th>`).join("");
      html += `</tr></thead><tbody>`;
      m.tickers.forEach((rowT, i) => {
        html += `<tr><th>${escapeHtml(rowT)}</th>`;
        m.values[i].forEach((v, j) => {
          html += `<td style="${i === j ? "" : corrCellStyle(v)}">${i === j ? "—" : (v ?? "—")}</td>`;
        });
        html += `</tr>`;
      });
      html += `</tbody></table></div></div>`;
    }
    html += `</div>`;
  }

  const missing = (data.tickers_without_data || []);
  html += `<p class="detail-disclaimer">Liczone na ostatnim roku notowań dla OBECNYCH wag pozycji (hipotetyczny portfel o stałych wagach, nie Twoja faktyczna historia), w walutach notowania - bez wpływu kursów walut. Sharpe przy stopie wolnej od ryzyka ${data.risk_free_rate_pct}% (zmiana: portfolio.risk_free_rate_pct w config.yaml). Uwzględnia ${data.tickers.length} pozycji${missing.length ? `; pominięto (brak danych): ${escapeHtml(missing.join(", "))}` : ""}. Porównanie z benchmarkiem dotyczy tego samego hipotetycznego portfela (nie krzywej kapitału z bazy, która zawiera Twoje wpłaty i sprzedaże). Przeszłość nie gwarantuje przyszłości.</p>`;

  panel.innerHTML = html;
  drawBenchmarkChart(data.benchmark_comparison);
}

// ---------------- Kopia zapasowa (import) ----------------
el("backupImportFile").addEventListener("change", () => {
  const file = el("backupImportFile").files[0];
  el("backupFileNameLabel").textContent = file ? `✅ ${file.name}` : "📂 Wybierz plik kopii (.json)";
});

el("backupImportBtn").addEventListener("click", async () => {
  const fileInput = el("backupImportFile");
  const file = fileInput.files[0];
  if (!file) {
    alert("Wybierz plik kopii zapasowej (.json).");
    return;
  }
  const btn = el("backupImportBtn");
  btn.disabled = true;
  btn.textContent = "Wczytuję…";

  const formData = new FormData();
  formData.append("file", file);
  try {
    const res = await fetch("/api/backup/import", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      alert(data.detail || "Nie udało się wczytać kopii.");
      return;
    }
    const msg = `Wczytano: ${data.positions_added} pozycji, ${data.watchlist_added} spółek, ${data.dividends_added ?? 0} dywidend i ${(data.targets_set ?? 0) + (data.sector_targets_set ?? 0)} celów alokacji. ` +
      `Pominięto duplikaty: ${data.positions_skipped} pozycji, ${data.watchlist_skipped} spółek, ${data.dividends_skipped ?? 0} dywidend.`;
    appendLog({ level: "success", message: `📤 ${msg}` });
    (data.warnings || []).forEach((w) => appendLog({ level: "warning", message: `📤 ${w}` }));
    alert(msg + (data.warnings && data.warnings.length ? `\n\nUwagi:\n${data.warnings.slice(0, 5).join("\n")}` : ""));
    fileInput.value = "";
    el("backupFileNameLabel").textContent = "📂 Wybierz plik kopii (.json)";
    await loadPortfolio();
    await loadClosedPortfolio();
    await loadDividends();
    await loadRebalancing();
  } catch (err) {
    alert("Błąd połączenia podczas wczytywania kopii.");
  } finally {
    btn.disabled = false;
    btn.textContent = "📤 Wczytaj kopię";
  }
});

// ---------------- Personalizacja kolumn (watchlista / propozycje AI) ----------------
// Kolejność musi zgadzać się z kolejnością elementów w DOM (renderResultCard,
// nagłówki main/discovered) - "chart" zawsze jest ostatnią opcjonalną kolumną
// przed kategorią, bo tak dokłada ją setupSparklineHeaders().
const COLUMN_PREFS_KEY = "columnPrefs";
const COLUMN_DEFAULTS = { target: true, signal: true, score: true, chart: true };
const COLUMN_SLOTS = [
  { key: "target", cssVar: "--col-target" },
  { key: "signal", cssVar: "--col-signal" },
  { key: "score", cssVar: "--col-score" },
  { key: "chart", cssVar: "--col-chart" },
];

function loadColumnPreferences() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(COLUMN_PREFS_KEY) || "{}");
  } catch {
    saved = {};
  }
  state.columnPrefs = { ...COLUMN_DEFAULTS, ...saved };
}

function saveColumnPreferences() {
  try {
    localStorage.setItem(COLUMN_PREFS_KEY, JSON.stringify(state.columnPrefs));
  } catch {
    // localStorage niedostępny (np. tryb prywatny) - personalizacja po prostu
    // nie przetrwa do następnej wizyty, reszta dashboardu działa bez zmian.
  }
}

// Składa listę torów siatki (--grid-cols-user) z atomowych zmiennych CSS,
// pomijając te odznaczone przez użytkownika - stamp/nazwa/kategoria są
// zawsze pierwsze/ostatnie i nigdy nie znikają.
function computeGridTemplate(prefs) {
  const visible = COLUMN_SLOTS.filter((s) => prefs[s.key] !== false).map((s) => `var(${s.cssVar})`);
  return ["var(--col-stamp)", "var(--col-name)", ...visible, "var(--col-category)"].join(" ");
}

function applyColumnVisibility(prefs) {
  document.documentElement.style.setProperty("--grid-cols-user", computeGridTemplate(prefs));
  // Chowamy TYLKO w obrębie watchlisty/propozycji AI (nagłówek ma klasę
  // results-header--customizable; portfel jej celowo nie ma) - inne miejsca
  // używające tego samego atrybutu (gdyby kiedyś powstały) zostają nietknięte.
  document
    .querySelectorAll('.results-header--customizable [data-col], #resultsGrid [data-col], #discoveredGrid [data-col]')
    .forEach((elx) => {
      elx.style.display = prefs[elx.dataset.col] === false ? "none" : "";
    });
}

function setupColumnSettings() {
  const btn = el("columnSettingsBtn");
  const popover = el("columnSettingsPopover");
  if (!btn || !popover) return;

  const checkboxes = {
    target: el("colToggleTarget"),
    signal: el("colToggleSignal"),
    score: el("colToggleScore"),
    chart: el("colToggleChart"),
  };

  function syncCheckboxes() {
    Object.entries(checkboxes).forEach(([key, input]) => { if (input) input.checked = state.columnPrefs[key] !== false; });
  }

  function closePopover() {
    popover.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  }
  function openPopover() {
    syncCheckboxes();
    popover.hidden = false;
    btn.setAttribute("aria-expanded", "true");
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.hidden ? openPopover() : closePopover();
  });
  popover.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", closePopover);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopover(); });

  Object.entries(checkboxes).forEach(([key, input]) => {
    if (!input) return;
    input.addEventListener("change", () => {
      // Zabezpieczenie: nie pozwalamy schować WSZYSTKICH opcjonalnych kolumn
      // naraz - karta bez żadnej z nich (tylko nazwa i kategoria) traci sens,
      // a użytkownik mógłby się pogubić, skąd nagle "zniknęły dane".
      const nextPrefs = { ...state.columnPrefs, [key]: input.checked };
      if (Object.values(nextPrefs).every((v) => v === false)) {
        input.checked = true;
        return;
      }
      state.columnPrefs = nextPrefs;
      saveColumnPreferences();
      applyColumnVisibility(state.columnPrefs);
    });
  });

  const resetBtn = el("columnSettingsReset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      state.columnPrefs = { ...COLUMN_DEFAULTS };
      saveColumnPreferences();
      applyColumnVisibility(state.columnPrefs);
      syncCheckboxes();
    });
  }
}

// ---------------- Personalizacja panelu bocznego (zwijanie, kolejność) ----------------
// Działa na KAŻDYM panelu bocznym osobno (dziś: Analiza i Portfel) - kolejność
// jest per-zakładka (klucz z ID rodzica .layout), zwinięcie per-panel (globalny
// klucz wg data-panel-id - ID paneli nie powtarzają się między zakładkami, więc
// nie ma potrzeby dodatkowego prefiksu).
const PANEL_COLLAPSE_PREFIX = "panelCollapsed:";

function sidebarOrderKey(sidebar) {
  const tabId = sidebar.closest(".layout")?.id || "default";
  return `sidebarPanelOrder:${tabId}`;
}

function updatePanelMoveButtons(sidebar) {
  const panels = Array.from(sidebar.querySelectorAll(":scope > section[data-panel-id]"));
  panels.forEach((panel, i) => {
    const up = panel.querySelector(".panel__move-up");
    const down = panel.querySelector(".panel__move-down");
    if (up) up.classList.toggle("is-disabled", i === 0);
    if (down) down.classList.toggle("is-disabled", i === panels.length - 1);
  });
}

function saveSidebarOrder(sidebar) {
  const order = Array.from(sidebar.querySelectorAll(":scope > section[data-panel-id]"))
    .map((p) => p.dataset.panelId);
  try {
    localStorage.setItem(sidebarOrderKey(sidebar), JSON.stringify(order));
  } catch {
    // jw. - brak trwałości nie jest błędem krytycznym
  }
}

function initPanelCustomizationForSidebar(sidebar) {
  // 1) Kolejność - zapisana lista ID paneli, z zabezpieczeniem na wypadek
  // przyszłych zmian w zestawie paneli (nieznane ID pomijamy, nowe panele,
  // których nie było w zapisanej kolejności, lądują na końcu w kolejności z HTML).
  let savedOrder = [];
  try {
    savedOrder = JSON.parse(localStorage.getItem(sidebarOrderKey(sidebar)) || "[]");
  } catch {
    savedOrder = [];
  }
  const panelsById = new Map(
    Array.from(sidebar.querySelectorAll(":scope > section[data-panel-id]")).map((p) => [p.dataset.panelId, p])
  );
  if (savedOrder.length) {
    savedOrder.forEach((id) => {
      const panel = panelsById.get(id);
      if (panel) sidebar.appendChild(panel);
    });
    // Panele nieobecne w zapisanej kolejności (np. dodane w nowszej wersji) -
    // dopisz na końcu, żeby nigdy nie zniknęły z widoku.
    panelsById.forEach((panel, id) => { if (!savedOrder.includes(id)) sidebar.appendChild(panel); });
  }

  // 2) Zwinięcie - niezależne od kolejności, jeden klucz per panel.
  panelsById.forEach((panel, id) => {
    if (localStorage.getItem(PANEL_COLLAPSE_PREFIX + id) === "1") {
      panel.classList.add("is-collapsed");
    }
  });

  // 3) Obsługa przycisków.
  panelsById.forEach((panel, id) => {
    const collapseBtn = panel.querySelector(".panel__collapse-toggle");
    if (collapseBtn) {
      collapseBtn.addEventListener("click", () => {
        const collapsed = panel.classList.toggle("is-collapsed");
        try {
          localStorage.setItem(PANEL_COLLAPSE_PREFIX + id, collapsed ? "1" : "0");
        } catch {
          // brak trwałości - dashboard nadal działa, tylko nie zapamięta stanu
        }
      });
    }
    const upBtn = panel.querySelector(".panel__move-up");
    if (upBtn) {
      upBtn.addEventListener("click", () => {
        const prev = panel.previousElementSibling;
        if (prev) {
          sidebar.insertBefore(panel, prev);
          saveSidebarOrder(sidebar);
          updatePanelMoveButtons(sidebar);
        }
      });
    }
    const downBtn = panel.querySelector(".panel__move-down");
    if (downBtn) {
      downBtn.addEventListener("click", () => {
        const next = panel.nextElementSibling;
        if (next) {
          sidebar.insertBefore(next, panel);
          saveSidebarOrder(sidebar);
          updatePanelMoveButtons(sidebar);
        }
      });
    }
  });

  updatePanelMoveButtons(sidebar);
}

function initPanelCustomization() {
  document.querySelectorAll(".layout .sidebar").forEach(initPanelCustomizationForSidebar);
}

// ---------------- Przeciąganie paneli (drag & drop) ----------------
// Alternatywa dla przycisków ▲/▼ (initPanelCustomization) - te zostają
// nietknięte jako dostępny, precyzyjny fallback. Technika "FLIP": przy
// każdej zmianie pozycji placeholdera zapisujemy stare współrzędne
// pozostałych paneli, wykonujemy reorder w DOM, po czym animujemy PANELE
// (nie placeholder) z ich starej pozycji do nowej - bez tego reorder
// flexboksa po prostu "przeskakiwałby" bez animacji.
function initPanelDragReorderForSidebar(sidebar) {
  const getPanels = () => Array.from(sidebar.querySelectorAll(":scope > section[data-panel-id]"));

  getPanels().forEach((panel) => {
    const handle = panel.querySelector(".panel__drag-handle");
    if (!handle) return;

    handle.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      e.preventDefault();

      const rect = panel.getBoundingClientRect();
      const offsetX = e.clientX - rect.left;
      const offsetY = e.clientY - rect.top;

      const spacer = document.createElement("div");
      spacer.className = "panel-drag-spacer";
      spacer.style.height = `${rect.height}px`;
      panel.after(spacer);

      panel.classList.add("is-dragging");
      Object.assign(panel.style, {
        position: "fixed",
        left: `${rect.left}px`,
        top: `${rect.top}px`,
        width: `${rect.width}px`,
        zIndex: "200",
        margin: "0",
      });

      const onMove = (ev) => {
        panel.style.left = `${ev.clientX - offsetX}px`;
        panel.style.top = `${ev.clientY - offsetY}px`;

        const siblings = Array.from(sidebar.children).filter((c) => c !== panel);
        let target = null;
        for (const sib of siblings) {
          const r = sib.getBoundingClientRect();
          if (ev.clientY < r.top + r.height / 2) { target = sib; break; }
        }
        if (target === spacer) return;
        if (target && target.previousElementSibling === spacer) return;

        // Wyklucza sam przeciągany panel - jego pozycja to left/top (fixed),
        // nie flexbox, więc nie powinien brać udziału w animacji FLIP reszty.
        const others = getPanels().filter((p) => p !== panel);
        const oldRects = new Map(others.map((p) => [p, p.getBoundingClientRect()]));

        if (target) sidebar.insertBefore(spacer, target);
        else sidebar.appendChild(spacer);

        others.forEach((p) => {
          const oldRect = oldRects.get(p);
          const newRect = p.getBoundingClientRect();
          const dy = oldRect.top - newRect.top;
          if (!dy) return;
          p.style.transition = "none";
          p.style.transform = `translateY(${dy}px)`;
          requestAnimationFrame(() => {
            p.style.transition = "transform 0.22s var(--ease)";
            p.style.transform = "";
            // Bez tego inline "transition: transform" zostałoby na stałe i
            // nadpisywało pełną listę przejść z .panel (border-color/box-
            // shadow na hover) po każdym przeciągnięciu - czyścimy inline
            // style, gdy animacja się skończy, żeby reguła z arkusza znów
            // przejęła kontrolę.
            p.addEventListener("transitionend", () => { p.style.transition = ""; }, { once: true });
          });
        });
      };

      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        panel.classList.remove("is-dragging");
        Object.assign(panel.style, {
          position: "", left: "", top: "", width: "", zIndex: "", margin: "",
        });
        sidebar.insertBefore(panel, spacer);
        spacer.remove();
        saveSidebarOrder(sidebar);
        updatePanelMoveButtons(sidebar);
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
    });
  });
}

function initPanelDragReorder() {
  document.querySelectorAll(".layout .sidebar").forEach(initPanelDragReorderForSidebar);
}

// ---------------- Zwijalne sekcje głównej kolumny (wszystkie zakładki) ----------------
// Prostszy wariant personalizacji niż panele boczne (bez przeciągania/kolejności) -
// tu chodzi głównie o odzyskanie miejsca na ekranie ("zajmuje za dużo miejsca"),
// nie o przestawianie. Przycisk zwijania jest dopisywany do .section-header przez
// JS (nie na sztywno w HTML), żeby nie duplikować identycznego znacznika w kilku
// miejscach index.html.
const DASH_SECTION_COLLAPSE_PREFIX = "dashSectionCollapsed:";

function initDashSections() {
  document.querySelectorAll(".dash-section[data-section-id]").forEach((section) => {
    const header = section.querySelector(":scope > .section-header");
    if (!header) return;
    const id = section.dataset.sectionId;
    const key = DASH_SECTION_COLLAPSE_PREFIX + id;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dash-section__toggle";
    btn.title = "Zwiń lub rozwiń sekcję";
    btn.setAttribute("aria-label", "Zwiń lub rozwiń sekcję");
    btn.textContent = "▾";
    header.appendChild(btn);

    if (localStorage.getItem(key) === "1") section.classList.add("is-collapsed");

    btn.addEventListener("click", () => {
      const collapsed = section.classList.toggle("is-collapsed");
      try {
        localStorage.setItem(key, collapsed ? "1" : "0");
      } catch {
        // brak trwałości - dashboard nadal działa, tylko nie zapamięta stanu
      }
      if (!collapsed) {
        // Sekcja mogła zawierać wykres (Lightweight Charts), który przy
        // tworzeniu/aktualizacji liczy szerokość z clientWidth kontenera -
        // gdy sekcja była zwinięta (display:none), ten kontener miał 0px.
        // Odpalenie tego samego handlera co przy zmianie rozmiaru okna każe
        // istniejącym wykresom przeliczyć się na nowo, teraz gdy są widoczne.
        requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
      }
    });
  });
}

// ---------------- Start ----------------
(async function init() {
  // Zabezpieczenie: modal, szufladka logu i widget czatu MUSZĄ być
  // bezpośrednimi dziećmi <body> - patrz komentarz przy poprzednich
  // naprawach tego samego problemu (zagnieżdżenie w display:none).
  document.body.appendChild(el("chartModal"));
  document.body.appendChild(el("logDrawer"));
  document.body.appendChild(el("chatWidget"));

  loadColumnPreferences();
  applyColumnVisibility(state.columnPrefs);
  setupColumnSettings();
  initPanelCustomization();
  initPanelDragReorder();
  initDashSections();

  setupSparklineHeaders();
  setupSortableHeaders();
  await Promise.all([loadWatchlist(), loadResults(), loadLogs(), loadStatus(), loadEffectiveness()]);
  await refreshHeldTickers();
  connectWebSocket();
  updateNextRunLabel();
})();
