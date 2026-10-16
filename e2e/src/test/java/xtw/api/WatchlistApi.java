package xtw.api;

import com.google.gson.JsonArray;
import com.google.gson.JsonParser;
import com.microsoft.playwright.APIRequestContext;
import com.microsoft.playwright.APIResponse;
import com.microsoft.playwright.options.RequestOptions;
import xtw.Config;

import java.util.Map;

/**
 * Cienki wrapper na wywołania /api/watchlist - testy wołają metody zamiast
 * budować URL-e/body ręcznie, ten sam pomysł co komponenty w xtw.components
 * dla UI, tylko dla warstwy HTTP.
 */
public class WatchlistApi {
    private final APIRequestContext request;

    public WatchlistApi(APIRequestContext request) {
        this.request = request;
    }

    public APIResponse list() {
        return request.get(Config.BASE_URL + "/api/watchlist");
    }

    public APIResponse add(String ticker, String name) {
        return request.post(Config.BASE_URL + "/api/watchlist",
            RequestOptions.create().setData(Map.of("ticker", ticker, "name", name)));
    }

    public APIResponse remove(String ticker) {
        return request.delete(Config.BASE_URL + "/api/watchlist/" + ticker);
    }

    public JsonArray listTickers() {
        return JsonParser.parseString(list().text()).getAsJsonArray();
    }

    public boolean containsTicker(String ticker) {
        for (var el : listTickers()) {
            if (el.getAsJsonObject().get("ticker").getAsString().equals(ticker)) return true;
        }
        return false;
    }
}
