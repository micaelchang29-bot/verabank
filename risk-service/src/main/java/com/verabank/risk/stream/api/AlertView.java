package com.verabank.risk.stream.api;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;
import java.util.List;

/** {@code Alert} de contracts/openapi.yaml. */
@JsonInclude(JsonInclude.Include.ALWAYS)
public record AlertView(
        @JsonProperty("alert_id") String alertId,
        @JsonProperty("rule_code") String ruleCode,
        @JsonProperty("rule_version") int ruleVersion,
        @JsonProperty("rule_params") JsonNode ruleParams,
        @JsonProperty("severity") String severity,
        @JsonProperty("status") String status,
        @JsonProperty("subject_account_id") String subjectAccountId,
        @JsonProperty("transfer_ids") List<String> transferIds,
        @JsonProperty("total_amount") String totalAmount,
        @JsonProperty("window_start") String windowStart,
        @JsonProperty("window_end") String windowEnd,
        @JsonProperty("created_at") String createdAt,
        @JsonProperty("closed_at") String closedAt,
        @JsonProperty("closed_by") String closedBy) {
}
