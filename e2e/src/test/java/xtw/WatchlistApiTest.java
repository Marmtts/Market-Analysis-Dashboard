package xtw;

import com.microsoft.playwright.APIRequestContext;
import com.microsoft.playwright.junit.UsePlaywright;
import org.junit.jupiter.api.Test;
import xtw.api.WatchlistApi;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

// Czyste testy HTTP (bez przeglądarki) przeciwko temu samemu izolowanemu
// serwerowi co testy UI (patrz run-tests.sh) - szybsze niż Playwright na
// stronie, bo pomijają renderowanie/DOM, ale sprawdzają więcej niż unit
// testy (prawdziwe żądanie -> prawdziwy FastAPI -> prawdziwa, izolowana
// baza). Ticker inny niż w WatchlistTest (UI), żeby oba na pewno nie
// kolidowały, nawet gdyby kiedyś zaczęły lecieć równolegle.
@UsePlaywright
class WatchlistApiTest {
    private static final String TICKER = "ZZAPITEST";

    @Test
    void canAddAndRemoveTickerViaApi(APIRequestContext request) {
        WatchlistApi watchlist = new WatchlistApi(request);

        var addResponse = watchlist.add(TICKER, "Test API Spółka");
        assertEquals(200, addResponse.status());
        assertTrue(watchlist.containsTicker(TICKER));

        var removeResponse = watchlist.remove(TICKER);
        assertEquals(200, removeResponse.status());
        assertFalse(watchlist.containsTicker(TICKER));
    }

    @Test
    void addingDuplicateTickerReturnsConflict(APIRequestContext request) {
        WatchlistApi watchlist = new WatchlistApi(request);
        try {
            assertEquals(200, watchlist.add(TICKER, "Test API Spółka").status());
            assertEquals(409, watchlist.add(TICKER, "Test API Spółka").status());
        } finally {
            watchlist.remove(TICKER);
        }
    }

    @Test
    void removingUnknownTickerReturnsNotFound(APIRequestContext request) {
        WatchlistApi watchlist = new WatchlistApi(request);
        assertEquals(404, watchlist.remove("NIEISTNIEJACYTICKER").status());
    }
}
