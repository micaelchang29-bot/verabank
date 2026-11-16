package com.verabank.transfer.saga;

/** Sentido de una respuesta del rail (docs/saga.md §5). */
public enum ReplyKind {
    /** CreditConfirmed, o CreditStatus CONFIRMED. */
    CONFIRM,
    /** CreditRejected, o CreditStatus REJECTED / CANCELLED. */
    REJECT,
    /** CreditStatus UNKNOWN: sin cambio de estado. */
    UNKNOWN
}
