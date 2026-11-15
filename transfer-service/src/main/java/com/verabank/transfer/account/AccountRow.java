package com.verabank.transfer.account;

import java.math.BigDecimal;
import java.util.UUID;

/** Fila de core.accounts. */
public record AccountRow(
        UUID accountId,
        UUID customerId,
        String accountType,
        String currency,
        String status,
        BigDecimal balance,
        BigDecimal held) {

    public boolean isCustomer() {
        return "CUSTOMER".equals(accountType);
    }

    public boolean isActive() {
        return "ACTIVE".equals(status);
    }

    public BigDecimal available() {
        return balance.subtract(held);
    }
}
