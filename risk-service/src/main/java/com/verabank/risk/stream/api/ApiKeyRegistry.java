package com.verabank.risk.stream.api;

import com.verabank.risk.shared.RiskConfig;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
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

/**
 * Claves por canal con la misma convención que transfer-service ({@code VERABANK_API_KEYS=canal:clave,canal:clave}).
 * En memoria solo se guarda el SHA-256 de cada clave; ningún log ni mensaje de error la incluye. El canal es el
 * {@code closed_by} de las alertas que se cierran.
 */
@ApplicationScoped
public class ApiKeyRegistry {

    private static final Pattern CLIENT_ID = Pattern.compile("[a-z0-9][a-z0-9_-]{0,63}");
    static final int MIN_KEY_LENGTH = 16;

    record Entry(byte[] keyHash, String client) {
    }

    @Inject
    RiskConfig config;

    private volatile List<Entry> entries;

    /** Valida la configuración al arrancar el rol stream: sin claves, el servicio no arranca. */
    public void init() {
        entries = parse(config.apiKeys().orElse(null));
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

    /** Canal dueño de la clave, si es válida. */
    public Optional<String> resolve(String presentedKey) {
        List<Entry> current = entries;
        if (current == null || presentedKey == null || presentedKey.isEmpty() || presentedKey.length() > 512) {
            return Optional.empty();
        }
        byte[] presented = sha256(presentedKey);
        // Recorre todas con comparación en tiempo constante: el tiempo no revela cuál clave (ni si alguna) es válida.
        String match = null;
        for (Entry entry : current) {
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
