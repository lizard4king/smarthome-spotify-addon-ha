ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.22
FROM ${BUILD_FROM}

WORKDIR /app
# Keyring uses PyPI packages; avoid the optional HA wheel mirror's long outages.
RUN PIP_CONFIG_FILE=/dev/null PIP_EXTRA_INDEX_URL= pip install --index-url https://pypi.org/simple --no-cache-dir --timeout 30 --retries 3 "keyring>=25,<26"

COPY smarthome /app/smarthome
COPY addon_entrypoint.py /app/addon_entrypoint.py
COPY config/spotify_profiles.example.json /app/config/spotify_profiles.example.json
COPY config/spotify_targets.example.json /app/config/spotify_targets.example.json
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
