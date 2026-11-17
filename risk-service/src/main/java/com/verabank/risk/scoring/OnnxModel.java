package com.verabank.risk.scoring;

import ai.onnxruntime.NodeInfo;
import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtException;
import ai.onnxruntime.OrtSession;
import ai.onnxruntime.TensorInfo;
import java.util.Map;
import java.util.Set;

/**
 * Una sesión de ONNX Runtime en CPU con un hilo intra-op. {@code OrtSession.run} admite llamadas concurrentes;
 * el servicio la ejecuta en el pool de workers, nunca en el event loop.
 */
class OnnxModel implements AutoCloseable {

    private final OrtEnvironment env;
    private final OrtSession session;
    private final String inputName;
    private final String outputName;
    private final int positiveClassColumn;

    private OnnxModel(OrtEnvironment env, OrtSession session, String inputName, String outputName, int column) {
        this.env = env;
        this.session = session;
        this.inputName = inputName;
        this.outputName = outputName;
        this.positiveClassColumn = column;
    }

    /** Carga el modelo y comprueba su contrato de entrada y salida (manifest.json:model_io). */
    static OnnxModel load(byte[] bytes, String inputName, String outputName, int column, int nFeatures)
            throws OrtException {
        OrtEnvironment env = OrtEnvironment.getEnvironment();
        try (OrtSession.SessionOptions options = new OrtSession.SessionOptions()) {
            options.setIntraOpNumThreads(1);
            options.setInterOpNumThreads(1);
            options.setExecutionMode(OrtSession.SessionOptions.ExecutionMode.SEQUENTIAL);
            OrtSession session = env.createSession(bytes, options);
            try {
                NodeInfo input = session.getInputInfo().get(inputName);
                if (input == null || session.getInputInfo().size() != 1) {
                    throw new IllegalStateException("el modelo no tiene la entrada única '" + inputName + "'");
                }
                long[] shape = ((TensorInfo) input.getInfo()).getShape();
                if (shape.length != 2 || shape[1] != nFeatures) {
                    throw new IllegalStateException("la entrada del modelo no es [N, " + nFeatures + "]");
                }
                if (!session.getOutputInfo().containsKey(outputName)) {
                    throw new IllegalStateException("el modelo no tiene la salida '" + outputName + "'");
                }
            } catch (RuntimeException e) {
                session.close();
                throw e;
            }
            return new OnnxModel(env, session, inputName, outputName, column);
        }
    }

    /** Probabilidad de la clase positiva, promovida de float32 a double (exacta). */
    double score(float[] row) {
        try (OnnxTensor input = OnnxTensor.createTensor(env, new float[][] {row});
                OrtSession.Result result = session.run(Map.of(inputName, input), Set.of(outputName))) {
            float[][] probabilities = (float[][]) result.get(outputName).orElseThrow().getValue();
            return probabilities[0][positiveClassColumn];
        } catch (OrtException e) {
            throw new IllegalStateException("falló la inferencia de ONNX Runtime", e);
        }
    }

    @Override
    public void close() {
        try {
            session.close();
        } catch (OrtException e) {
            throw new IllegalStateException(e);
        }
    }
}
