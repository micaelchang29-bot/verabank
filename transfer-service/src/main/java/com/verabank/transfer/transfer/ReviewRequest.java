package com.verabank.transfer.transfer;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

/** Cuerpo de {@code POST /api/v1/transfers/{id}/review}. */
public record ReviewRequest(
        @NotNull ReviewDecision decision,
        @NotNull @Size(min = 1, max = 1000) String comment) {
}
