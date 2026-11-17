# Rail interbancario simulado para Compose (perfil sim). Contexto: la RAÍZ del repositorio.
#   docker build -f simulator/Dockerfile -t verabank/rail-sim:dev .
FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.13.8
RUN useradd --system --uid 10003 --create-home rail
WORKDIR /app/simulator
COPY simulator/pyproject.toml simulator/uv.lock simulator/README.md ./
COPY simulator/verabank_sim verabank_sim
COPY contracts/events /app/contracts/events
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev && mkdir /state && chown rail /state
USER 10003
ENV PATH=/opt/venv/bin:$PATH VERABANK_CONTRACTS_DIR=/app/contracts/events RAIL_STATE_DB=/state/rail.db
ENTRYPOINT ["python", "-m", "verabank_sim", "rail"]
