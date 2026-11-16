package com.verabank.transfer.saga;

/** Valores de core.saga_state.status y current_step (docs/saga.md §2). */
public final class SagaStates {

    public static final String RUNNING = "RUNNING";
    public static final String IN_REVIEW = "IN_REVIEW";
    public static final String WAITING_REPLY = "WAITING_REPLY";
    public static final String COMPENSATING = "COMPENSATING";
    public static final String COMPLETED = "COMPLETED";
    public static final String COMPENSATED = "COMPENSATED";
    public static final String COMPENSATION_FAILED = "COMPENSATION_FAILED";

    public static final String FUNDS_HELD = "FUNDS_HELD";
    public static final String CREDIT_REQUESTED = "CREDIT_REQUESTED";
    public static final String CANCEL_REQUESTED = "CANCEL_REQUESTED";
    public static final String DONE = "DONE";

    private SagaStates() {
    }
}
