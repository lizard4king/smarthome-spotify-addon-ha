FROM python:3.12-alpine3.22

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOCALAPPDATA=/data

COPY code/finance_control /app/finance_control
COPY run.sh /app/run.sh
RUN chmod 0755 /app/run.sh

EXPOSE 8785
ENTRYPOINT ["/app/run.sh"]
