# CI: el build de la imagen de risk-service falló al descargar la distribución de Maven
- id: 20261119-0615-ci-maven-central
- paso: 6 (cierre)
- estado: resuelto
- tipo: configuración (CI)
- límite del plan que tocó: ninguno
## Error
Ejecución 44512163319 (commit 6249757): job «humo con Compose» en rojo. En «Imágenes con caché de capas», `./mvnw
package` dentro del Dockerfile de risk-service: `java.io.FileNotFoundException:
https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.16/apache-maven-3.9.16-bin.zip`. Después, el
paso de limpieza falló con «required variable KAFKA_CLUSTER_ID is missing», porque `.env` aún no existía.
## Causa
Descarga intermitente desde `repo.maven.apache.org` en los runners: la misma URL respondió bien en las ejecuciones
anteriores y en local, y es el mismo origen que hacía fallar la descarga del `maven-wrapper.jar` (ahora versionado). La
distribución de Maven no se versiona como el jar (la instala el wrapper dentro de la imagen). La limpieza llamaba a
`make reset-data` sin comprobar que Compose se hubiera levantado.
## Solución
`.github/workflows/ci.yml`: el build de cada imagen se reintenta hasta 3 veces con 20 s de espera (un error de
compilación real falla las tres); la limpieza solo corre si existe `.env`. Un primer intento del arreglo (commit
0e657ca) dejó un escalar YAML inválido en la limpieza («sin .env: …» sin comillas), que actionlint detectó; se corrigió
con un bloque `run: |` en el commit siguiente.
## Verificación
actionlint sin errores; resultado de la CI posterior en `docs/resultados/fase-5.md` (cierre).
## Pendiente para el usuario
Si la intermitencia persiste, un espejo de Maven de confianza (`MVNW_REPOURL`) o una caché de `~/.m2/wrapper` en el
build de las imágenes.
