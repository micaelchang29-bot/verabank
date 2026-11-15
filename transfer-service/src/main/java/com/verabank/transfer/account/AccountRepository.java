package com.verabank.transfer.account;

import static com.verabank.transfer.config.Sql.uuid;

import java.math.BigDecimal;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class AccountRepository {

    private static final String COLUMNS =
            "account_id, customer_id, account_type, currency, status, balance, held";

    private final JdbcClient jdbc;

    public AccountRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<AccountRow> find(UUID accountId) {
        return jdbc.sql("SELECT " + COLUMNS + " FROM core.accounts WHERE account_id = :id")
                .param("id", accountId)
                .query(AccountRepository::map)
                .optional();
    }

    /** Bloquea las cuentas en orden de account_id: dos cierres cruzados no se interbloquean. */
    public List<AccountRow> lockInOrder(UUID... accountIds) {
        List<UUID> ids = List.of(accountIds).stream().distinct().sorted(Comparator.naturalOrder()).toList();
        return jdbc.sql("SELECT " + COLUMNS + " FROM core.accounts WHERE account_id IN (:ids)"
                        + " ORDER BY account_id FOR UPDATE")
                .param("ids", ids)
                .query(AccountRepository::map)
                .list();
    }

    /**
     * Retención con UPDATE condicional (§2.3): solo si la cuenta está activa y el disponible
     * cubre el monto. Dos retenciones concurrentes se serializan en el bloqueo de la fila y la
     * segunda reevalúa la condición con el saldo ya retenido.
     */
    public boolean hold(UUID accountId, BigDecimal amount) {
        return jdbc.sql("""
                UPDATE core.accounts SET held = held + :amount, version = version + 1
                WHERE account_id = :id AND status = 'ACTIVE' AND balance - held >= :amount""")
                .param("id", accountId)
                .param("amount", amount)
                .update() == 1;
    }

    public void release(UUID accountId, BigDecimal amount) {
        int n = jdbc.sql("""
                UPDATE core.accounts SET held = held - :amount, version = version + 1
                WHERE account_id = :id AND held >= :amount""")
                .param("id", accountId)
                .param("amount", amount)
                .update();
        if (n != 1) {
            throw new IllegalStateException("la cuenta " + accountId + " no tiene retenido " + amount);
        }
    }

    /** Débito que consume la retención previa; devuelve el saldo resultante. */
    public BigDecimal debitHeld(UUID accountId, BigDecimal amount) {
        return jdbc.sql("""
                UPDATE core.accounts
                SET balance = balance - :amount, held = held - :amount, version = version + 1
                WHERE account_id = :id AND held >= :amount
                RETURNING balance""")
                .param("id", accountId)
                .param("amount", amount)
                .query(BigDecimal.class)
                .optional()
                .orElseThrow(() -> new IllegalStateException("la cuenta " + accountId + " no tiene retenido " + amount));
    }

    public BigDecimal credit(UUID accountId, BigDecimal amount) {
        return jdbc.sql("""
                UPDATE core.accounts SET balance = balance + :amount, version = version + 1
                WHERE account_id = :id
                RETURNING balance""")
                .param("id", accountId)
                .param("amount", amount)
                .query(BigDecimal.class)
                .single();
    }

    private static AccountRow map(ResultSet rs, int row) throws SQLException {
        return new AccountRow(uuid(rs, "account_id"), uuid(rs, "customer_id"), rs.getString("account_type"),
                rs.getString("currency"), rs.getString("status"), rs.getBigDecimal("balance"),
                rs.getBigDecimal("held"));
    }
}
