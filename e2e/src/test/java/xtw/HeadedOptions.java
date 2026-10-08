package xtw;

import com.microsoft.playwright.BrowserType;
import com.microsoft.playwright.junit.Options;
import com.microsoft.playwright.junit.OptionsFactory;

/**
 * Włącza widoczną przeglądarkę + spowolnienie akcji - do oglądania testu na
 * żywo. W CI (zmienna env CI, ustawiana automatycznie przez GitHub Actions)
 * wraca do headless+bez slowMo - na runnerze nie ma ekranu, a headed tam by
 * się po prostu wywalił.
 */
public class HeadedOptions implements OptionsFactory {
    @Override
    public Options getOptions() {
        boolean ci = System.getenv("CI") != null;
        return new Options().setHeadless(ci)
            .setLaunchOptions(new BrowserType.LaunchOptions().setSlowMo(ci ? 0 : 2000));
    }
}
