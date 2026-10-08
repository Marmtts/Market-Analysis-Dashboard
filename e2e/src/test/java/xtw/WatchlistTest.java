package xtw;

import com.microsoft.playwright.Page;
import com.microsoft.playwright.junit.UsePlaywright;
import org.junit.jupiter.api.Test;
import xtw.pages.WatchlistPanel;

import static com.microsoft.playwright.assertions.PlaywrightAssertions.assertThat;

@UsePlaywright(HeadedOptions.class)
class WatchlistTest {
    // ponytail: przy odpaleniu przez run-tests.sh baza jest tymczasowa i tak
    // czy inaczej kasowana po teście - to ryzyko dotyczy tylko ręcznego
    // `mvn test -DbaseUrl=...` wskazanego na DŁUGO ŻYJĄCY serwer (np. własny,
    // ręcznie odpalony na test-config.yaml): jeśli test padnie MIĘDZY
    // dodaniem a usunięciem, ticker zostanie tam na stałe.
    private static final String TICKER = "ZZTEST";

    @Test
    void canAddAndRemoveTicker(Page page) {
        page.navigate(Config.BASE_URL);
        WatchlistPanel watchlist = new WatchlistPanel(page);

        watchlist.addTicker(TICKER, "Test Spółka");
        assertThat(watchlist.row(TICKER)).isVisible();

        watchlist.removeTicker(TICKER);
        assertThat(watchlist.row(TICKER)).isHidden();
    }
}
