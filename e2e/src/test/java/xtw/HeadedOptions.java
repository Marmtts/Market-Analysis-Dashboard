package xtw;

import com.microsoft.playwright.BrowserType;
import com.microsoft.playwright.junit.Options;
import com.microsoft.playwright.junit.OptionsFactory;

/** Włącza widoczną przeglądarkę + spowolnienie akcji - do oglądania testu na żywo. */
public class HeadedOptions implements OptionsFactory {
    @Override
    public Options getOptions() {
        return new Options().setHeadless(false)
            .setLaunchOptions(new BrowserType.LaunchOptions().setSlowMo(2000));
    }
}
