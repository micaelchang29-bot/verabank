# risk-service: una imagen, el rol se elige al arrancar con VERABANK_ROLE=scoring|stream (ADR-08, D3).
# El contexto es la RAÍZ del repositorio (necesita ml/artifacts y contracts/events):
#   docker build -f risk-service/Dockerfile -t verabank/risk-service:dev .      (make build-risk)

FROM eclipse-temurin:21-jdk AS build
WORKDIR /repo
COPY risk-service/mvnw risk-service/pom.xml risk-service/
COPY risk-service/.mvn risk-service/.mvn
# Los artefactos del modelo se leen de ml/artifacts al compilar (D2); no hay copias en el repositorio.
COPY ml/artifacts/model.onnx ml/artifacts/thresholds.json ml/artifacts/manifest.json ml/artifacts/
COPY ml/artifacts/challenger/model.onnx ml/artifacts/challenger/
# Esquemas de eventos que valida el rol stream (una sola copia, la de contracts/; viajan dentro del jar).
COPY contracts/events/envelope.v1.json contracts/events/TransferCompleted.v1.schema.json contracts/events/FraudLabelConfirmed.v1.schema.json contracts/events/
COPY risk-service/src risk-service/src
RUN cd risk-service && ./mvnw -B -ntp -q -DskipTests package

FROM eclipse-temurin:21-jre
RUN useradd --system --uid 10001 --no-create-home risk
WORKDIR /app
COPY --from=build /repo/risk-service/target/quarkus-app/ /app/
USER 10001
# Memoria acotada por configuración (se puede cambiar con -e JAVA_OPTS=...): montículo de 256 MB y topes para lo que
# queda fuera de él. ONNX Runtime reserva memoria nativa aparte, por eso MaxDirectMemorySize y el margen.
ENV JAVA_OPTS="-Xms64m -Xmx256m -XX:MaxMetaspaceSize=128m -XX:ReservedCodeCacheSize=64m -XX:MaxDirectMemorySize=64m -XX:+ExitOnOutOfMemoryError"
ENV VERABANK_ROLE=scoring SERVER_PORT=8082
EXPOSE 8082
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/quarkus-run.jar"]
