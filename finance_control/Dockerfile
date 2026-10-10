FROM python:3.12-alpine3.22

ARG BUILD_VERSION=0.2.30
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOCALAPPDATA=/data \
    TZ=Europe/Berlin

COPY finance_control /app/finance_control
COPY build-manifest.json /app/build-manifest.json
COPY bootstrap.py /app/bootstrap.py
COPY vault_selftest.py /app/vault_selftest.py
COPY run.sh /app/run.sh
RUN apk add --no-cache tzdata && \
  pip install --no-cache-dir openpyxl==3.1.5 defusedxml==0.7.1 'PyJWT[crypto]==2.15.1' fints==5.0.0 mt-940==5.1.1 tzdata==2026.5 && \
  chmod 0755 /app/run.sh && \
  python -c "import time; from datetime import UTC, datetime; stamp=datetime(2026,9,30,22,30,tzinfo=UTC).timestamp(); assert time.localtime(stamp)[:5] == (2026,10,1,0,30)" && \
  python /app/vault_selftest.py && \
  python -c "import json; assert json.load(open('/app/build-manifest.json', encoding='utf-8'))['addon_version'] == '${BUILD_VERSION}'"

EXPOSE 8785
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json, urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8785/health', timeout=3); raise SystemExit(0 if r.status == 200 and json.loads(r.read()) == {'status': 'ok'} else 1)"
ENTRYPOINT ["/app/run.sh"]
