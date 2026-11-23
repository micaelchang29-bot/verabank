package com.verabank.risk.scoring;

import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import org.junit.jupiter.api.Test;

/** D16, primer calentamiento con conexiones que llegan tarde (ver {@link WarmUpRecovery}). */
@QuarkusTest
@TestProfile(WarmUpRecovery.SomeConnectionsArriveLate.class)
class WarmUpAfterLateConnectionsIT {

    @Test
    void readinessRecoversAfterTheWarmUpTimedOut() {
        WarmUpRecovery.assertReadinessRecovers();
    }
}
