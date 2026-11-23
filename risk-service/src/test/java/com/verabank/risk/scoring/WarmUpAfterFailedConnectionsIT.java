package com.verabank.risk.scoring;

import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import org.junit.jupiter.api.Test;

/** D16, primer calentamiento con conexiones que fallan (ver {@link WarmUpRecovery}). */
@QuarkusTest
@TestProfile(WarmUpRecovery.SomeConnectionsFail.class)
class WarmUpAfterFailedConnectionsIT {

    @Test
    void readinessRecoversAfterSomeConnectionsFailed() {
        WarmUpRecovery.assertReadinessRecovers();
    }
}
