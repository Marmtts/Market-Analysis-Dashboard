package xtw;

import com.microsoft.playwright.Page;
import com.microsoft.playwright.junit.UsePlaywright;
import org.junit.jupiter.api.Test;
import xtw.components.DashboardTabs;
import xtw.components.PortfolioPanel;

import static com.microsoft.playwright.assertions.PlaywrightAssertions.assertThat;

@UsePlaywright(HeadedOptions.class)
class PortfolioTest {
    // ponytail: tak jak w WatchlistTest - ryzyko osieroconej pozycji dotyczy
    // tylko ręcznego odpalenia przeciwko długo żyjącemu serwerowi.
    private static final String TICKER = "ZZPORTUI";

    @Test
    void canAddAndDeletePosition(Page page) {
        page.navigate(Config.BASE_URL);
        new DashboardTabs(page).openAndVerify(DashboardTabs.Tab.PORTFOLIO);

        PortfolioPanel portfolio = new PortfolioPanel(page);
        portfolio.addPosition(TICKER, "10", "100", "2024-01-15");

        assertThat(portfolio.group(TICKER)).isVisible();

        portfolio.expand(TICKER);
        portfolio.deleteFirstLot(TICKER);

        assertThat(portfolio.group(TICKER)).isHidden();
    }
}
