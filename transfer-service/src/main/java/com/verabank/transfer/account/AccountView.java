package com.verabank.transfer.account;

import java.math.RoundingMode;
import java.util.UUID;

/** Account de contracts/openapi.yaml: montos como cadena decimal; available = balance - held. */
public record AccountView(
        UUID accountId,
        String status,
        String currency,
        String balance,
        String held,
        String available) {

    public static AccountView of(AccountRow a) {
        return new AccountView(a.accountId(), a.status(), a.currency(), money(a.balance()), money(a.held()),
                money(a.available()));
    }

    private static String money(java.math.BigDecimal value) {
        return value.setScale(2, RoundingMode.UNNECESSARY).toPlainString();
    }
}
