FROM ollama/ollama:0.40.13 AS ollama
RUN rm -rf /usr/lib/ollama/cuda* /usr/lib/ollama/rocm* /usr/lib/ollama/vulkan* /usr/lib/ollama/mlx*
FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates libgomp1 libopenblas0 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --omit=dev --ignore-scripts @openai/codex@0.160.0 \
    && test "$(codex --version)" = 'codex-cli 0.160.0' \
    && groupadd --gid 10001 finance-ai \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin finance-ai
COPY --from=ollama /bin/ollama /usr/local/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama
RUN /usr/local/bin/ollama --version
WORKDIR /app
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=/data/models \
    OLLAMA_NO_CLOUD=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 \
    CODEX_HOME=/data/codex HOME=/data/home
COPY assistant_engine.py assistant_runtime.py assistant_login.py /app/finance_control/
RUN touch /app/finance_control/__init__.py
COPY server.py launcher.py /app/
EXPOSE 8786
CMD ["python3", "/app/launcher.py"]
