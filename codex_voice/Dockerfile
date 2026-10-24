# Reconstructed from server recipe, backup history and recovered application.
FROM node:22-bookworm-slim@sha256:25330af3531fb5e23318554a0aa911125b6e91b1b777edf7655501d207c067a2

ARG BUILD_VERSION=0.1.11
ARG BUILD_ARCH=amd64

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 codex \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin codex \
    && npm install --global --omit=dev --ignore-scripts @openai/codex@0.160.0 \
    && test "$(codex --version)" = "codex-cli 0.160.0" \
    && features="$(codex features list)" \
    && for feature in apps auth_elicitation browser_use browser_use_external browser_use_full_cdp_access code_mode code_mode_host computer_use goals image_generation in_app_browser in_app_chat in_app_local_automation memories multi_agent plugins realtime_conversation remote_plugin shell_tool skill_mcp_dependency_install skill_search sleep_tool tool_call_mcp_elicitation tool_suggest unified_exec view_image; do \
         printf '%s\n' "$features" | grep -Eq "^${feature}[[:space:]]" || exit 1; \
       done \
    && printf '%s\n' "$features" | grep -Eq '^apply_patch_freeform[[:space:]]+removed[[:space:]]+false$'

WORKDIR /app
COPY __init__.py app.py voice_bridge.py control_client.py action_schema.json ./

RUN python3 -c 'import subprocess; from app import make_query_command; command = make_query_command(structured=True); command.insert(-1, "--help"); subprocess.run(command, check=True, stdout=subprocess.DEVNULL)'

CMD ["python3", "/app/app.py"]
