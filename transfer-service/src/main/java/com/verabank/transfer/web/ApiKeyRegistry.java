package com.verabank.transfer.web;

import com.verabank.transfer.config.VerabankProperties;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;
import org.springframework.stereotype.Component;

/**
 * Claves por canal (§1.3). Configuración: {@code verabank.api-keys = canal:clave,canal:clave}.
 * El canal es el {@code client_id} de la unicidad de idempotencia.
 *
 * <p>En memoria solo se guarda el SHA-256 de cada clave, y ningún mensaje de error ni log
 * incluye la clave.
 */
@Component
public class ApiKeyRegistry {

    private static final Pattern CLIENT_ID = Pattern.compile("[a-z0-9][a-z0-9_-]{0,63}");
    static final int MIN_KEY_LENGTH = 16;

    private record Entry(byte[] keyHash, String client) {
    }

    private final List<Entry> entries;

    public ApiKeyRegistry(VerabankProperties properties) {
        this.entries = parse(properties.apiKeys());
    }

    static List<Entry> parse(String spec) {
        if (spec == null || spec.isBlank()) {
            throw new IllegalStateException(
                    "verabank.api-keys (VERABANK_API_KEYS) está vacío: define al menos un canal como canal:clave");
        }
        List<Entry> result = new ArrayList<>();
        Set<String> clients = new HashSet<>();
        Set<String> hashes = new HashSet<>();
        int position = 0;
        for (String entry : spec.split(",")) {
            position++;
            String trimmed = entry.strip();
            int sep = trimmed.indexOf(':');
            if (sep <= 0) {
                throw new IllegalStateException("verabank.api-keys: la entrada " + position
                        + " no tiene el formato canal:clave");
            }
            String client = trimmed.substring(0, sep);
            String key = trimmed.substring(sep + 1);
            if (!CLIENT_ID.matcher(client).matches()) {
                throw new IllegalStateException("verabank.api-keys: canal inválido en la entrada " + position
                        + " (minúsculas, dígitos, '_' o '-')");
            }
            if (key.length() < MIN_KEY_LENGTH) {
                throw new IllegalStateException("verabank.api-keys: la clave del canal " + client
                        + " debe tener al menos " + MIN_KEY_LENGTH + " caracteres");
            }
            if (!clients.add(client)) {
                throw new IllegalStateException("verabank.api-keys: canal repetido " + client);
            }
            byte[] hash = sha256(key);
            if (!hashes.add(HexFormat.of().formatHex(hash))) {
                throw new IllegalStateException("verabank.api-keys: dos canales comparten clave");
            }
            result.add(new Entry(hash, client));
        }
        return List.copyOf(result);
    }

    /** Canal dueño de la clave, si la clave es válida. */
    public Optional<String> resolve(String presentedKey) {
        if (presentedKey == null || presentedKey.isEmpty() || presentedKey.length() > 512) {
            return Optional.empty();
        }
        byte[] presented = sha256(presentedKey);
        // Se recorren todos los hashes con comparación en tiempo constante, sin salir en el
        // primero que coincide: el tiempo no depende de cuál clave (ni de si alguna) es válida.
        String match = null;
        for (Entry entry : entries) {
            if (MessageDigest.isEqual(entry.keyHash(), presented)) {
                match = entry.client();
            }
        }
        return Optional.ofNullable(match);
    }

    private static byte[] sha256(String value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
