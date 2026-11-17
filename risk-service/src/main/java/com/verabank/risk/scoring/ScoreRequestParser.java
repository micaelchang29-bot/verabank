package com.verabank.risk.scoring;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.shared.ApiException;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.time.temporal.ChronoUnit;
import java.util.UUID;
import java.util.regex.Pattern;

/** Valida el cuerpo según ScoreRequest de contracts/openapi.yaml; cualquier defecto es un 400 validation-error. */
final class ScoreRequestParser {

    private static final Pattern DECIMAL = Pattern.compile("^[0-9]{1,16}(\\.[0-9]{1,2})?$");
    private static final Pattern CURRENCY = Pattern.compile("^[A-Z]{3}$");
    private static final Pattern COUNTRY = Pattern.compile("^[A-Z]{2}$");

    private final ObjectMapper mapper;

    ScoreRequestParser(ObjectMapper mapper) {
        this.mapper = mapper.copy().disable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
    }

    ScoreInput parse(String body) {
        JsonNode root;
        try {
            root = body == null || body.isBlank() ? null : mapper.readTree(body);
        } catch (JsonProcessingException e) {
            throw ApiException.validation("El cuerpo no es un JSON válido");
        }
        if (root == null || !root.isObject()) {
            throw ApiException.validation("El cuerpo debe ser un objeto JSON");
        }
        String transferId = text(root, "transfer_id");
        UUID id;
        try {
            id = UUID.fromString(transferId);
        } catch (IllegalArgumentException e) {
            throw ApiException.validation("transfer_id debe ser un UUID");
        }
        String sourceRef = nonEmpty(root, "source_ref");
        String destRef = nonEmpty(root, "dest_ref");
        String amount = text(root, "amount");
        if (!DECIMAL.matcher(amount).matches()) {
            throw ApiException.validation("amount debe ser una cadena decimal con hasta 2 decimales");
        }
        String currency = text(root, "currency");
        if (!CURRENCY.matcher(currency).matches()) {
            throw ApiException.validation("currency debe tener 3 letras mayúsculas");
        }
        String type = text(root, "transfer_type");
        if (!FeatureComputer.TYPES.contains(type)) {
            throw ApiException.validation("transfer_type debe ser uno de " + FeatureComputer.TYPES);
        }
        String country = text(root, "dest_country");
        if (!COUNTRY.matcher(country).matches()) {
            throw ApiException.validation("dest_country debe tener 2 letras mayúsculas");
        }
        Instant occurredAt;
        try {
            occurredAt = OffsetDateTime.parse(text(root, "occurred_at")).toInstant().truncatedTo(ChronoUnit.MICROS);
        } catch (DateTimeParseException e) {
            throw ApiException.validation("occurred_at debe ser una fecha-hora ISO 8601 con zona");
        }
        return new ScoreInput(id, sourceRef, destRef, new BigDecimal(amount), currency, type, country, occurredAt);
    }

    private static String text(JsonNode root, String field) {
        JsonNode node = root.get(field);
        if (node == null || node.isNull()) {
            throw ApiException.validation("Falta " + field);
        }
        if (!node.isTextual()) {
            throw ApiException.validation(field + " debe ser una cadena");
        }
        return node.asText();
    }

    private static String nonEmpty(JsonNode root, String field) {
        String value = text(root, field);
        if (value.isEmpty()) {
            throw ApiException.validation(field + " no puede estar vacío");
        }
        return value;
    }
}
