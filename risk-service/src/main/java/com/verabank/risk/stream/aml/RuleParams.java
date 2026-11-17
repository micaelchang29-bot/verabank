package com.verabank.risk.stream.aml;

import com.fasterxml.jackson.databind.JsonNode;
import java.math.BigDecimal;

/** Parámetros de una versión activa de {@code risk.aml_rules}: versión, JSON original y lectura exacta de decimales. */
public record RuleParams(String ruleCode, int version, String json, JsonNode node) {

    /** Decimal exacto; admite número o cadena ({@code "10000.00"}). */
    public BigDecimal decimal(String name) {
        JsonNode value = require(name);
        return value.isTextual() ? new BigDecimal(value.asText()) : value.decimalValue();
    }

    public int integer(String name) {
        return require(name).intValue();
    }

    private JsonNode require(String name) {
        JsonNode value = node.get(name);
        if (value == null || value.isNull()) {
            throw new IllegalStateException("la regla " + ruleCode + " v" + version + " no define el parámetro " + name);
        }
        return value;
    }
}
