package com.verabank.transfer.transfer;

/** Valores de {@code reason} de TransferRejected (contracts/events). */
public enum RejectionReason {
    INSUFFICIENT_FUNDS, RISK_BLOCK, ANALYST_REJECT, ACCOUNT_NOT_ACTIVE
}
