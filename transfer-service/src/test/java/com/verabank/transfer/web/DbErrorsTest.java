package com.verabank.transfer.web;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.SQLException;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.springframework.dao.CannotAcquireLockException;
import org.springframework.dao.CannotSerializeTransactionException;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.dao.DeadlockLoserDataAccessException;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.QueryTimeoutException;
import org.springframework.dao.RecoverableDataAccessException;
import org.springframework.dao.TransientDataAccessResourceException;
import org.springframework.jdbc.BadSqlGrammarException;
import org.springframework.jdbc.CannotGetJdbcConnectionException;
import org.springframework.jdbc.UncategorizedSQLException;
import org.springframework.transaction.CannotCreateTransactionException;
import org.springframework.transaction.TransactionSystemException;

class DbErrorsTest {

    @Test
    void transitorios() {
        Stream.<Throwable>of(
                new CannotSerializeTransactionException("40001"),
                new DeadlockLoserDataAccessException("40P01", null),
                new CannotAcquireLockException("lock"),
                new QueryTimeoutException("timeout"),
                new TransientDataAccessResourceException("transitorio"),
                new RecoverableDataAccessException("recuperable"),
                new DataAccessResourceFailureException("recursos"),
                new CannotGetJdbcConnectionException("sin conexión", new SQLException("down")),
                new CannotCreateTransactionException("no se pudo abrir la transacción"))
                .forEach(e -> assertThat(DbErrors.isTransient(e)).as(e.getClass().getSimpleName()).isTrue());
    }

    @Test
    void permanentes() {
        Stream.<Throwable>of(
                new DataIntegrityViolationException("23514"),
                new DuplicateKeyException("23505"),
                new UncategorizedSQLException("tarea", "SELECT 1", new SQLException("P0001")),
                new BadSqlGrammarException("tarea", "SELEC 1", new SQLException("42601")),
                new TransactionSystemException("commit"),
                new IllegalStateException("invariante"),
                new RuntimeException("otro"))
                .forEach(e -> assertThat(DbErrors.isTransient(e)).as(e.getClass().getSimpleName()).isFalse());
    }
}
