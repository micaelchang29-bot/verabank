# transfer-service para Compose (perfil risk). El contexto es la RAÍZ del repositorio (necesita db/migrations y contracts/events):
#   docker build -f transfer-service/Dockerfile -t verabank/transfer-service:dev .
FROM eclipse-temurin:21-jdk AS build
WORKDIR /repo
COPY transfer-service/mvnw transfer-service/pom.xml transfer-service/
COPY transfer-service/.mvn transfer-service/.mvn
COPY transfer-service/src transfer-service/src
RUN cd transfer-service && ./mvnw -B -ntp -q -DskipTests package

FROM eclipse-temurin:21-jre
RUN useradd --system --uid 10002 --no-create-home transfer
WORKDIR /app
COPY --from=build /repo/transfer-service/target/transfer-service.jar /app/transfer-service.jar
# Migraciones y esquemas se leen de estos directorios (VERABANK_MIGRATIONS_DIR, VERABANK_CONTRACTS_DIR), sin copias en el jar.
COPY db/migrations /app/db/migrations
COPY contracts/events /app/contracts/events
USER 10002
ENV JAVA_OPTS="-Xms128m -Xmx384m -XX:MaxMetaspaceSize=160m -XX:ReservedCodeCacheSize=64m -XX:MaxDirectMemorySize=64m -XX:+ExitOnOutOfMemoryError"
ENV SERVER_PORT=8081 VERABANK_MIGRATIONS_DIR=/app/db/migrations VERABANK_CONTRACTS_DIR=/app/contracts
EXPOSE 8081
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/transfer-service.jar"]
