# ADR-05 · Modelo en ONNX dentro del servicio Java

- Contexto: inferencia en el camino crítico de 150 ms.
- Opciones: servicio Python (FastAPI); ONNX Runtime en `risk-service`; XGBoost4J.
- Decisión: ONNX Runtime. Python entrena y exporta; Java sirve.
- Consecuencias: un salto de red menos y un solo formato para ambos modelos. Riesgo de divergencia entre entrenamiento y servicio, cubierto con pruebas de paridad. ONNX Runtime usa JNI: modo JVM, sin imagen nativa.
