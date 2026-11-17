package com.verabank.risk.stream.api;

import java.util.Map;
import java.util.Set;

/** Máquina de estados de una alerta (D4): OPEN → IN_REVIEW → ESCALATED | CLOSED_*; ESCALATED → CLOSED_*. */
public final class AlertTransitions {

    public static final Set<String> STATUSES = Set.of("OPEN", "IN_REVIEW", "ESCALATED", "CLOSED_FALSE_POSITIVE",
            "CLOSED_CONFIRMED");
    public static final Set<String> SEVERITIES = Set.of("LOW", "MEDIUM", "HIGH");

    private static final Map<String, Set<String>> ALLOWED = Map.of(
            "OPEN", Set.of("IN_REVIEW"),
            "IN_REVIEW", Set.of("ESCALATED", "CLOSED_FALSE_POSITIVE", "CLOSED_CONFIRMED"),
            "ESCALATED", Set.of("CLOSED_FALSE_POSITIVE", "CLOSED_CONFIRMED"));

    private AlertTransitions() {
    }

    public static boolean allowed(String from, String to) {
        return ALLOWED.getOrDefault(from, Set.of()).contains(to);
    }

    public static boolean closes(String status) {
        return status.startsWith("CLOSED_");
    }
}
