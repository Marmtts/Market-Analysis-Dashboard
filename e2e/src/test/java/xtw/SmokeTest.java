package xtw;

import com.microsoft.playwright.Page;
import com.microsoft.playwright.junit.UsePlaywright;
import org.junit.jupiter.api.Test;
import xtw.components.DashboardTabs;

import static com.microsoft.playwright.assertions.PlaywrightAssertions.assertThat;

@UsePlaywright(HeadedOptions.class)
class SmokeTest {
    @Test
    void dashboardLoads(Page page) {
        page.navigate(Config.BASE_URL);
        assertThat(page).hasTitle("XTB Trend Watch — Terminal");
    }

    @Test
    void allTabsOpenCorrectly(Page page) {
        page.navigate(Config.BASE_URL);
        DashboardTabs tabs = new DashboardTabs(page);

        // Analiza jest domyślnie otwarta na starcie - mimo to zaczynamy od
        // przejścia na inną zakładkę i wracamy, żeby sprawdzić realne
        // przełączenie, nie tylko stan początkowy.
        tabs.openAndVerify(DashboardTabs.Tab.PORTFOLIO);
        tabs.openAndVerify(DashboardTabs.Tab.CLOSED);
        tabs.openAndVerify(DashboardTabs.Tab.ANALYSIS);
    }
}
