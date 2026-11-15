package com.verabank.transfer.support;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Datos de prueba con las mismas reglas que db/seed: el saldo inicial entra como transferencia
 * INTERNAL COMPLETED desde la cuenta CLEARING, con su par de asientos, para que la conciliación
 * de §7 dé 0 en todo momento.
 */
public final class Bank {

    public static final UUID CLEARING = UUID.nameUUIDFromBytes("verabank-test:clearing".getBytes(StandardCharsets.UTF_8));

    private final JdbcClient jdbc;
    private final TransactionTemplate tx;

    public Bank(JdbcClient jdbc, PlatformTransactionManager txManager) {
        this.jdbc = jdbc;
        this.tx = new TransactionTemplate(txManager);
        jdbc.sql("""
                INSERT INTO core.accounts (account_id, customer_id, account_type, currency, status)
                VALUES (:id, NULL, 'CLEARING', 'PEN', 'ACTIVE') ON CONFLICT (account_id) DO NOTHING""")
                .param("id", CLEARING)
                .update();
    }

    /** Cliente con una cuenta activa en PEN y el saldo indicado. */
    public UUID account(String balance) {
        return account(balance, "ACTIVE");
    }

    public UUID account(String balance, String status) {
        UUID customer = UUID.randomUUID();
        UUID account = UUID.randomUUID();
        BigDecimal amount = new BigDecimal(balance);
        tx.executeWithoutResult(s -> {
            jdbc.sql("INSERT INTO core.customers (customer_id, segment) VALUES (:id, 'RETAIL')")
                    .param("id", customer).update();
            jdbc.sql("""
                    INSERT INTO core.accounts (account_id, customer_id, account_type, currency, status)
                    VALUES (:id, :customer, 'CUSTOMER', 'PEN', 'ACTIVE')""")
                    .param("id", account).param("customer", customer).update();
            if (amount.signum() > 0) {
                fund(account, amount);
            }
            if (!"ACTIVE".equals(status)) {
                jdbc.sql("UPDATE core.accounts SET status = :status WHERE account_id = :id")
                        .param("status", status).param("id", account).update();
            }
        });
        return account;
    }

    private void fund(UUID account, BigDecimal amount) {
        UUID transfer = UUID.randomUUID();
        jdbc.sql("""
                INSERT INTO core.transfers (transfer_id, client_id, idempotency_key, request_hash,
                    source_account_id, dest_account_id, dest_country, rail, transfer_type, amount, currency,
                    status, occurred_at, completed_at)
                VALUES (:id, 'test-seed', :key, repeat('0', 64), :clearing, :account, 'PE', 'INTERNAL',
                    'TRANSFER', :amount, 'PEN', 'COMPLETED', :at, :at)""")
                .param("id", transfer).param("key", UUID.randomUUID()).param("clearing", CLEARING)
                .param("account", account).param("amount", amount)
                .param("at", OffsetDateTime.ofInstant(Instant.now(), ZoneOffset.UTC))
                .update();
        BigDecimal clearingAfter = jdbc.sql("""
                UPDATE core.accounts SET balance = balance - :amount, version = version + 1
                WHERE account_id = :id RETURNING balance""")
                .param("amount", amount).param("id", CLEARING).query(BigDecimal.class).single();
        BigDecimal accountAfter = jdbc.sql("""
                UPDATE core.accounts SET balance = balance + :amount, version = version + 1
                WHERE account_id = :id RETURNING balance""")
                .param("amount", amount).param("id", account).query(BigDecimal.class).single();
        entry(transfer, CLEARING, "D", amount, clearingAfter);
        entry(transfer, account, "C", amount, accountAfter);
    }

    private void entry(UUID transfer, UUID account, String direction, BigDecimal amount, BigDecimal after) {
        jdbc.sql("""
                INSERT INTO core.transactions (transfer_id, account_id, direction, entry_kind, amount, balance_after)
                VALUES (:t, :a, :d, 'TRANSFER', :amount, :after)""")
                .param("t", transfer).param("a", account).param("d", direction)
                .param("amount", amount).param("after", after).update();
    }

    public BigDecimal balance(UUID account) {
        return jdbc.sql("SELECT balance FROM core.accounts WHERE account_id = :id")
                .param("id", account).query(BigDecimal.class).single();
    }

    public BigDecimal held(UUID account) {
        return jdbc.sql("SELECT held FROM core.accounts WHERE account_id = :id")
                .param("id", account).query(BigDecimal.class).single();
    }

    public int countTransfers(String clientId, UUID idempotencyKey) {
        return jdbc.sql("SELECT count(*) FROM core.transfers WHERE client_id = :c AND idempotency_key = :k")
                .param("c", clientId).param("k", idempotencyKey).query(Integer.class).single();
    }

    public int countEntries(UUID transferId, String direction) {
        return jdbc.sql("SELECT count(*) FROM core.transactions WHERE transfer_id = :t AND direction = :d")
                .param("t", transferId).param("d", direction).query(Integer.class).single();
    }

    public int countEntriesForAccount(UUID accountId, String direction) {
        return jdbc.sql("SELECT count(*) FROM core.transactions WHERE account_id = :a AND direction = :d")
                .param("a", accountId).param("d", direction).query(Integer.class).single();
    }

    /** Las dos consultas de conciliación de §7 (db/reconcile.sql), en ese orden. */
    public long[] reconcile() {
        long unbalanced = jdbc.sql("""
                SELECT count(*) AS cuentas_descuadradas FROM core.accounts a
                WHERE a.balance <> COALESCE((SELECT sum(CASE direction WHEN 'C' THEN amount ELSE -amount END)
                                             FROM core.transactions x WHERE x.account_id = a.account_id), 0)""")
                .query(Long.class).single();
        long orphanHolds = jdbc.sql("""
                SELECT count(*) AS retenciones_huerfanas FROM core.accounts a
                WHERE a.held <> 0 AND NOT EXISTS (
                  SELECT 1 FROM core.transfers t
                  WHERE t.source_account_id = a.account_id AND t.status IN ('PENDING','IN_REVIEW','PROCESSING'))""")
                .query(Long.class).single();
        return new long[] {unbalanced, orphanHolds};
    }

    public static String internal(UUID source, UUID dest, String amount) {
        return """
                {"source_account_id":"%s","dest_account_id":"%s","dest_country":"PE","rail":"INTERNAL",
                 "transfer_type":"TRANSFER","amount":"%s","currency":"PEN","occurred_at":"2026-03-01T10:15:30Z"}"""
                .formatted(source, dest, amount);
    }
}
