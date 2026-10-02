ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.22
FROM ${BUILD_FROM}

WORKDIR /app
COPY dashboard /app/dashboard
COPY smarthome /app/smarthome
COPY config /app/config
COPY addon_entrypoint.py /app/addon_entrypoint.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8767
CMD ["/run.sh"]
