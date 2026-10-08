package xtw.pages;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/**
 * Komponent panelu watchlisty (lewy sidebar, zakładka Analiza) - patrz
 * #addForm/#watchlistUl w web/static/index.html. Testy wołają te metody
 * zamiast grzebać w selektorach bezpośrednio.
 */
public class WatchlistPanel {
    private final Page page;

    private final Locator form;

    public WatchlistPanel(Page page) {
        this.page = page;
        // Ten sam placeholder "Ticker (np. AAPL)" ma też pole w formularzu
        // rebalancingu (zakładka Portfel) - bez zawężenia do #addForm
        // Playwright odmawia działania w strict mode (locator pasujący do
        // >1 elementu to błąd, nie zgadywanie, który miałeś na myśli).
        this.form = page.locator("#addForm");
    }

    public void addTicker(String ticker, String name) {
        form.getByPlaceholder("Ticker (np. AAPL)").fill(ticker);
        form.getByPlaceholder("Nazwa spółki").fill(name);
        form.getByRole(AriaRole.BUTTON, new Locator.GetByRoleOptions().setName("+ Dodaj")).click();
    }

    public void removeTicker(String ticker) {
        row(ticker).getByRole(AriaRole.BUTTON).click(); // jedyny przycisk w wierszu - "✕ Usuń"
        // Własny dialog potwierdzenia (showConfirmDialog w app.js), nie natywny
        // confirm() przeglądarki - to zwykły element strony, klikalny jak każdy inny.
        page.getByRole(AriaRole.BUTTON, new Page.GetByRoleOptions().setName("Usuń").setExact(true)).click();
    }

    public Locator row(String ticker) {
        return page.locator(".watchlist__item").filter(new Locator.FilterOptions().setHasText(ticker));
    }
}
