package com.verabank.risk.scoring;

import ai.onnxruntime.OrtException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.shared.RiskConfig;
import io.quarkus.arc.lookup.LookupIfProperty;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.io.IOException;
import java.math.BigDecimal;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import org.jboss.logging.Logger;

/**
 * Carga y valida los artefactos de ml/artifacts al arrancar: SHA-256 de model.onnx contra thresholds.json y
 * contra manifest.json, SHA-256 de thresholds.json contra manifest.json y que feature_order sea exactamente el
 * conjunto que {@link FeatureComputer} sabe calcular. Cualquier diferencia deja el servicio sin modelo (no
 * listo); el log nombra qué falló, sin volcar el contenido de los archivos.
 */
@ApplicationScoped
@LookupIfProperty(name = "verabank.role", stringValue = "scoring")
class ModelRegistry {

    private static final Logger LOG = Logger.getLogger(ModelRegistry.class);
    private static final int DECIMALS = 5;

    @Inject
    ArtifactSource source;
    @Inject
    RiskConfig config;
    @Inject
    ScoringMetrics metrics;
    @Inject
    ObjectMapper objectMapper;

    private volatile ModelState state = ModelState.failed(List.of("los artefactos aún no se han cargado"));

    ModelState state() {
        return state;
    }

    void load() {
        state = doLoad();
        if (state.loaded()) {
            LOG.infof("modelo %s cargado (rol scoring, sombra %s)", state.champion().modelVersion(),
                    state.challenger() != null ? "activa" : "inactiva");
        } else {
            LOG.errorf("el servicio NO queda listo: artefactos del modelo inválidos: %s", state.problems());
        }
    }

    private ModelState doLoad() {
        List<String> problems = new ArrayList<>();
        ObjectMapper json = objectMapper.copy().enable(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS);
        byte[] modelBytes;
        byte[] thresholdBytes;
        JsonNode thresholds;
        JsonNode manifest;
        try {
            modelBytes = source.read("model.onnx");
            thresholdBytes = source.read("thresholds.json");
            thresholds = json.readTree(thresholdBytes);
            manifest = json.readTree(source.read("manifest.json"));
        } catch (IOException e) {
            return ModelState.failed(List.of("no se pudieron leer los artefactos: " + e.getMessage()));
        }

        Thresholds t;
        try {
            t = new Thresholds(
                    thresholds.required("model_version").asText(),
                    thresholds.required("model_sha256").asText(),
                    stringList(thresholds.required("feature_order")),
                    thresholds.required("t_review").decimalValue(),
                    thresholds.required("t_block").decimalValue(),
                    thresholds.required("rounding_decimals").asInt());
        } catch (IllegalArgumentException | NullPointerException e) {
            return ModelState.failed(List.of("thresholds.json incompleto: " + e.getMessage()));
        }

        String modelSha = sha256(modelBytes);
        if (!modelSha.equals(t.modelSha256())) {
            problems.add("model.onnx no coincide con thresholds.json:model_sha256 (" + abbreviate(modelSha) + " frente a "
                    + abbreviate(t.modelSha256()) + ")");
        }
        JsonNode artifacts = manifest.path("artifacts");
        compare(problems, "model.onnx", modelSha, artifacts.path("model.onnx").path("sha256").asText(""));
        compare(problems, "thresholds.json", sha256(thresholdBytes),
                artifacts.path("thresholds.json").path("sha256").asText(""));
        JsonNode champion = manifest.path("models").path("champion");
        String manifestVersion = champion.path("registered_model").asText("") + "/" + champion.path("version").asText("");
        if (!manifestVersion.equals(t.modelVersion())) {
            problems.add("thresholds.json:model_version (" + t.modelVersion() + ") no coincide con el champion de manifest.json ("
                    + manifestVersion + ")");
        }
        if (t.featureOrder().size() != new HashSet<>(t.featureOrder()).size()
                || !new HashSet<>(t.featureOrder()).equals(new HashSet<>(FeatureComputer.FEATURE_NAMES))) {
            problems.add("thresholds.json:feature_order no es el conjunto de features que el código sabe calcular");
        }
        if (t.roundingDecimals() != DECIMALS) {
            problems.add("rounding_decimals debe ser " + DECIMALS + " y es " + t.roundingDecimals());
        }
        if (t.reviewThreshold().signum() < 0 || t.reviewThreshold().compareTo(t.blockThreshold()) > 0
                || t.blockThreshold().compareTo(BigDecimal.ONE) > 0) {
            problems.add("los umbrales deben cumplir 0 <= t_review <= t_block <= 1");
        }
        JsonNode io = manifest.path("model_io");
        String inputName = io.path("input_name").asText("");
        String outputName = io.path("output_name").asText("");
        int column = io.path("positive_class_column").asInt(-1);
        if (inputName.isEmpty() || outputName.isEmpty() || column < 0) {
            problems.add("manifest.json:model_io incompleto");
        }
        if (!problems.isEmpty()) {
            return ModelState.failed(problems);
        }

        OnnxModel championModel;
        try {
            championModel = OnnxModel.load(modelBytes, inputName, outputName, column, t.featureOrder().size());
        } catch (OrtException | RuntimeException e) {
            return ModelState.failed(List.of("model.onnx no se pudo cargar: " + e.getMessage()));
        }
        ModelState.Champion loaded = new ModelState.Champion(championModel, t.modelVersion(), t.featureOrder(),
                new ThresholdDecisionPolicy(t.reviewThreshold(), t.blockThreshold(), t.roundingDecimals()));
        return new ModelState(loaded, config.shadow().enabled()
                ? loadChallenger(artifacts, inputName, outputName, column, t.featureOrder().size()) : null, List.of());
    }

    /** La sombra nunca afecta a la disponibilidad: si el challenger no es válido, se desactiva y se cuenta. */
    private OnnxModel loadChallenger(JsonNode artifacts, String input, String output, int column, int n) {
        try {
            byte[] bytes = source.read("challenger/model.onnx");
            String expected = artifacts.path("challenger/model.onnx").path("sha256").asText("");
            if (!sha256(bytes).equals(expected)) {
                throw new IllegalStateException("challenger/model.onnx no coincide con manifest.json ("
                        + abbreviate(sha256(bytes)) + " frente a " + abbreviate(expected) + ")");
            }
            return OnnxModel.load(bytes, input, output, column, n);
        } catch (IOException | OrtException | RuntimeException e) {
            LOG.errorf("no se pudo cargar el challenger; la sombra queda desactivada: %s", e.getMessage());
            metrics.shadowFailure();
            return null;
        }
    }

    private static void compare(List<String> problems, String name, String actual, String expected) {
        if (!actual.equals(expected)) {
            problems.add(name + " no coincide con manifest.json (" + abbreviate(actual) + " frente a " + abbreviate(expected) + ")");
        }
    }

    private static List<String> stringList(JsonNode array) {
        List<String> out = new ArrayList<>();
        array.forEach(n -> out.add(n.asText()));
        return List.copyOf(out);
    }

    static String sha256(byte[] bytes) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String abbreviate(String hash) {
        return hash.length() > 12 ? hash.substring(0, 12) + "…" : hash;
    }
}
