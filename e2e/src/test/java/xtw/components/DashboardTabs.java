package xtw.components;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

import static com.microsoft.playwright.assertions.PlaywrightAssertions.assertThat;

/**
 * Górny pasek zakładek (nav.tabs w index.html) - trzy przyciski przełączają
 * widoczność trzech <main id="tab-...">. Tylko jedna zakładka powinna być
 * widoczna naraz - stąd openAndVerify() sprawdza też, że POZOSTAŁE dwie są
 * ukryte, nie tylko że klikana się pokazała.
 */
public class DashboardTabs {
    public enum Tab {
        ANALYSIS("Analiza", "tab-analysis", "Stała watchlista"),
        PORTFOLIO("Portfel", "tab-portfolio", "Twój portfel"),
        CLOSED("Zamknięte transakcje", "tab-closed", "Zamknięte transakcje");

        final String buttonName;
        final String panelId;
        final String headingName;

        Tab(String buttonName, String panelId, String headingName) {
            this.buttonName = buttonName;
            this.panelId = panelId;
            this.headingName = headingName;
        }
    }

    private final Page page;

    public DashboardTabs(Page page) {
        this.page = page;
    }

    public void openAndVerify(Tab tab) {
        page.getByRole(AriaRole.BUTTON, new Page.GetByRoleOptions().setName(tab.buttonName)).click();

        Locator panel = page.locator("#" + tab.panelId);
        assertThat(panel).isVisible();
        assertThat(panel.getByRole(AriaRole.HEADING, new Locator.GetByRoleOptions().setName(tab.headingName)))
            .isVisible();

        for (Tab other : Tab.values()) {
            if (other != tab) {
                assertThat(page.locator("#" + other.panelId)).isHidden();
            }
        }
    }
}
