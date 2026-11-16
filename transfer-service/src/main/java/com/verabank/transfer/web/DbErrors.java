package com.verabank.transfer.web;

import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.dao.RecoverableDataAccessException;
import org.springframework.dao.TransientDataAccessException;
import org.springframework.transaction.CannotCreateTransactionException;

/**
 * Distingue errores de base de datos transitorios (503 reintentable) de permanentes (500).
 *
 * <p>Transitorio: reintentar la misma solicitud puede funcionar. Son los fallos de recursos o
 * de conexión ({@link DataAccessResourceFailureException}, que incluye
 * {@code CannotGetJdbcConnectionException}), las caídas recuperables
 * ({@link RecoverableDataAccessException}) y los transitorios de verdad
 * ({@link TransientDataAccessException}: timeouts, serialización {@code 40001}, deadlocks).
 * {@link CannotCreateTransactionException} es no poder abrir la transacción por no tener
 * conexión; no es un {@code DataAccessException} pero es el mismo caso.
 *
 * <p>Todo lo demás (integridad, SQL inválido, errores no clasificados) es permanente: un
 * reintento repetiría el fallo, así que no se anuncia como reintentable.
 */
public final class DbErrors {

    private DbErrors() {
    }

    public static boolean isTransient(Throwable error) {
        return error instanceof TransientDataAccessException
                || error instanceof RecoverableDataAccessException
                || error instanceof DataAccessResourceFailureException
                || error instanceof CannotCreateTransactionException;
    }
}
