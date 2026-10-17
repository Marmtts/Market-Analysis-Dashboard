package xtw.components;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/**
 * Komponent zakładki Portfel - formularz "Dodaj pozycję" (lewy sidebar) i
 * siatka pozycji (#portfolioGrid, prawa kolumna). Pozycje są grupowane wg
 * tickera (.portfolio-group) - nagłówek grupy (.portfolio-group__header)
 * pokazuje ticker i jest klikalny, żeby rozwinąć poszczególne "loty" (zakupy)
 * w środku (.portfolio-group__lots, domyślnie zwinięte - patrz
 * .portfolio-group.is-expanded w style.css).
 */
public class PortfolioPanel {
    private final Page page;
    private final Locator form;
    private final Locator grid;

    public PortfolioPanel(Page page) {
        this.page = page;
        this.form = page.locator("#portfolioForm");
        this.grid = page.locator("#portfolioGrid");
    }

    public void addPosition(String ticker, String shares, String buyPrice, String buyDate) {
        form.getByPlaceholder("Ticker (np. MSFT)").fill(ticker);
        form.getByPlaceholder("Liczba akcji").fill(shares);
        form.getByPlaceholder("Cena zakupu").fill(buyPrice);
        form.locator("#posBuyDate").fill(buyDate);
        form.getByRole(AriaRole.BUTTON, new Locator.GetByRoleOptions().setName("+ Dodaj pozycję")).click();
    }

    /** Nagłówek grupy pozycji dla danego tickera (zwinięty albo rozwinięty). */
    public Locator group(String ticker) {
        return grid.locator(".portfolio-group").filter(
            new Locator.FilterOptions().setHas(
                page.locator(".result-card__ticker").getByText(ticker, new Locator.GetByTextOptions().setExact(true))
            )
        );
    }

    public void expand(String ticker) {
        group(ticker).locator(".portfolio-group__header").click();
    }

    /** Usuwa PIERWSZY lot w (już rozwiniętej) grupie danego tickera, potwierdzając dialog. */
    public void deleteFirstLot(String ticker) {
        group(ticker).locator(".lot-delete").first().click();
        page.getByRole(AriaRole.BUTTON, new Page.GetByRoleOptions().setName("Usuń").setExact(true)).click();
    }
}
