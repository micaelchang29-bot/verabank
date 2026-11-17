package com.verabank.risk.stream.api;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/** {@code AlertPage} de contracts/openapi.yaml. */
@JsonInclude(JsonInclude.Include.ALWAYS)
public record AlertPage(@JsonProperty("items") List<AlertView> items, @JsonProperty("next_cursor") String nextCursor) {
}
