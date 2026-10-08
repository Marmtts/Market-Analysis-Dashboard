package xtw;

/**
 * Adres testowanego serwera - nadpisywalny przez `mvn test -DbaseUrl=...`
 * (patrz run-tests.sh). Domyślnie port 8123 (izolowana instancja testowa z
 * run-tests.sh), NIE 8000 (Twój prawdziwy, działający dashboard) - odpalenie
 * gołego `mvn test` bez skryptu ma po prostu nie połączyć się z niczym,
 * zamiast po cichu pisać/kasować w realnych danych.
 */
public final class Config {
    public static final String BASE_URL = System.getProperty("baseUrl", "http://localhost:8123");

    private Config() {}
}
