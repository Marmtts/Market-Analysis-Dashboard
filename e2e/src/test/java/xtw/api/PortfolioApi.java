package xtw.api;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.microsoft.playwright.APIRequestContext;
import com.microsoft.playwright.APIResponse;
import com.microsoft.playwright.options.RequestOptions;
import xtw.Config;

import java.util.HashMap;
import java.util.Map;

/**
 * Cienki wrapper na wywołania /api/portfolio - ten sam wzorzec co
 * xtw.api.WatchlistApi. Testy wołają metody zamiast budować URL-e/body
 * ręcznie.
 */
public class PortfolioApi {
    private final APIRequestContext request;

    public PortfolioApi(APIRequestContext request) {
        this.request = request;
    }

    public APIResponse list() {
        return request.get(Config.BASE_URL + "/api/portfolio");
    }

    public APIResponse add(String ticker, double shares, double buyPrice, String buyDate) {
        Map<String, Object> body = new HashMap<>();
        body.put("ticker", ticker);
        body.put("shares", shares);
        body.put("buy_price", buyPrice);
        body.put("buy_date", buyDate);
        return request.post(Config.BASE_URL + "/api/portfolio", RequestOptions.create().setData(body));
    }

    public APIResponse remove(int positionId) {
        return request.delete(Config.BASE_URL + "/api/portfolio/" + positionId);
    }

    public JsonArray listPositions() {
        return JsonParser.parseString(list().text()).getAsJsonArray();
    }

    /** Zwraca id pierwszej otwartej pozycji dla danego tickera, albo -1 gdy brak. */
    public int findPositionId(String ticker) {
        for (var el : listPositions()) {
            JsonObject pos = el.getAsJsonObject();
            if (pos.get("ticker").getAsString().equals(ticker)) {
                return pos.get("id").getAsInt();
            }
        }
        return -1;
    }

    public boolean containsTicker(String ticker) {
        return findPositionId(ticker) != -1;
    }
}
