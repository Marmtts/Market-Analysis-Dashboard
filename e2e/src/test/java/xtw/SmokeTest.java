package xtw;

import com.microsoft.playwright.Page;
import com.microsoft.playwright.junit.UsePlaywright;
import org.junit.jupiter.api.Test;

import static com.microsoft.playwright.assertions.PlaywrightAssertions.assertThat;

@UsePlaywright(HeadedOptions.class)
class SmokeTest {
    @Test
    void dashboardLoads(Page page) {
        page.navigate(Config.BASE_URL);
        assertThat(page).hasTitle("XTB Trend Watch — Terminal");
    }
}
