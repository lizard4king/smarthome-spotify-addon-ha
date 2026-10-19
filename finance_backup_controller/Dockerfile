ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-websocket-client
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY controller.py scheduler.py recovery.py /app/
# Deliberately use our own foreground process rather than an auto-restarting
# S6 service: an uncertain backup operation must stop, not blindly restart.
ENTRYPOINT ["/usr/bin/python3", "/app/scheduler.py"]
