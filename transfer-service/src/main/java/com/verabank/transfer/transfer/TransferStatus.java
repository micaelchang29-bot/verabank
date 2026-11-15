package com.verabank.transfer.transfer;

public enum TransferStatus {
    PENDING, IN_REVIEW, PROCESSING, COMPLETED, REJECTED, FAILED;

    public boolean isOpen() {
        return this == PENDING || this == IN_REVIEW || this == PROCESSING;
    }
}
