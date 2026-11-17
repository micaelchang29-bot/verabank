package com.verabank.risk.scoring;

import java.util.List;

/** Resultado de la carga de artefactos al arrancar: o todo es válido, o hay una lista de motivos. */
record ModelState(Champion champion, OnnxModel challenger, List<String> problems) {

    record Champion(OnnxModel model, String modelVersion, List<String> featureOrder, DecisionPolicy policy) {
    }

    static ModelState failed(List<String> problems) {
        return new ModelState(null, null, List.copyOf(problems));
    }

    boolean loaded() {
        return champion != null;
    }
}
