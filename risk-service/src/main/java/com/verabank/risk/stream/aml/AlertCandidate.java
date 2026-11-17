package com.verabank.risk.stream.aml;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

/** Alerta que una regla propone; la persiste {@link AlertRepository} con upsert por {@code dedupeKey}. */
public record AlertCandidate(String ruleCode, int ruleVersion, String ruleParams, String severity, String dedupeKey,
        UUID subjectAccountId, Instant windowStart, Instant windowEnd, BigDecimal totalAmount,
        List<UUID> transferIds) {
}
