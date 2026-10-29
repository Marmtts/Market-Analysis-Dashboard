package xtw.tests;

import com.microsoft.playwright.APIRequestContext;
import com.microsoft.playwright.junit.UsePlaywright;
import org.junit.jupiter.api.Test;
import xtw.api.PortfolioApi;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

// Czyste testy HTTP (bez przeglądarki) przeciwko /api/portfolio - ten sam
// wzorzec co WatchlistApiTest. GET /api/portfolio łączy pozycje z DB z
// pamięcią podręczną wyników analizy (patrz _get_enriched_open_positions w
// web_app.py) - NIE robi żadnych żywych zapytań sieciowych, więc jest
// bezpieczny i szybki w izolowanej instancji testowej.
@UsePlaywright
class PortfolioApiTest {
    private static final String TICKER = "ZZPORTAPI";

    @Test
    void canAddAndRemovePositionViaApi(APIRequestContext request) {
        PortfolioApi portfolio = new PortfolioApi(request);

        var addResponse = portfolio.add(TICKER, 10, 100.0, "2024-01-15");
        assertEquals(200, addResponse.status());
        assertTrue(portfolio.containsTicker(TICKER));

        int id = portfolio.findPositionId(TICKER);
        var removeResponse = portfolio.remove(id);
        assertEquals(200, removeResponse.status());
        assertFalse(portfolio.containsTicker(TICKER));
    }

    @Test
    void addingPositionWithZeroSharesIsRejected(APIRequestContext request) {
        PortfolioApi portfolio = new PortfolioApi(request);
        // web_app.py: "req.shares <= 0" -> 400, zanim cokolwiek trafi do bazy.
        assertEquals(400, portfolio.add(TICKER, 0, 100.0, "2024-01-15").status());
        assertFalse(portfolio.containsTicker(TICKER));
    }

    @Test
    void addingPositionWithZeroBuyPriceIsRejected(APIRequestContext request) {
        PortfolioApi portfolio = new PortfolioApi(request);
        assertEquals(400, portfolio.add(TICKER, 10, 0, "2024-01-15").status());
        assertFalse(portfolio.containsTicker(TICKER));
    }

    @Test
    void removingUnknownPositionReturnsNotFound(APIRequestContext request) {
        PortfolioApi portfolio = new PortfolioApi(request);
        assertEquals(404, portfolio.remove(999_999).status());
    }
}
