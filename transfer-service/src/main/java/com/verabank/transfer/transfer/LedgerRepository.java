package com.verabank.transfer.transfer;

import java.math.BigDecimal;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** Asientos de core.transactions (doble partida). uq_entry impide asentar dos veces. */
@Repository
public class LedgerRepository {

    private final JdbcClient jdbc;

    public LedgerRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public void post(UUID transferId, UUID accountId, char direction, String entryKind, BigDecimal amount,
            BigDecimal balanceAfter) {
        jdbc.sql("""
                INSERT INTO core.transactions (transfer_id, account_id, direction, entry_kind, amount, balance_after)
                VALUES (:transfer, :account, :direction, :kind, :amount, :balanceAfter)""")
                .param("transfer", transferId)
                .param("account", accountId)
                .param("direction", String.valueOf(direction))
                .param("kind", entryKind)
                .param("amount", amount)
                .param("balanceAfter", balanceAfter)
                .update();
    }
}
