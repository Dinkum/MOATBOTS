FROM python:3.12.10-slim-bookworm AS hermes-source

ARG HERMES_REF=fcbd1076a93841fa88855acce810e342a5b78101
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && git init /src/hermes-agent \
    && git -C /src/hermes-agent remote add origin \
        https://github.com/NousResearch/hermes-agent.git \
    && git -C /src/hermes-agent fetch --depth 1 origin "$HERMES_REF" \
    && git -C /src/hermes-agent checkout --detach FETCH_HEAD \
    && test "$(git -C /src/hermes-agent rev-parse HEAD)" = "$HERMES_REF"

FROM lscr.io/linuxserver/webtop:ubuntu-xfce@sha256:6a62904004f685b3a3eaeecfe022939c9349628c728f698f792ed4670009f8c6

ARG HERMES_REF=fcbd1076a93841fa88855acce810e342a5b78101
LABEL org.opencontainers.image.title="Moatbots Agent Computer"
LABEL org.opencontainers.image.description="Isolated Hermes computer for one Moatbots teammate"
LABEL io.moatbots.hermes-ref="$HERMES_REF"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_PYTHON_INSTALL_DIR=/opt/python

COPY --from=ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie@sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b /usr/local/bin/uv /usr/local/bin/uv
COPY --from=hermes-source /src/hermes-agent /opt/hermes-agent
RUN uv python install 3.12.10 \
    && UV_PROJECT_ENVIRONMENT=/opt/hermes-venv \
       uv sync --frozen --no-dev --extra mcp --project /opt/hermes-agent \
       --python 3.12.10 \
    && rm -rf /root/.cache/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg=7:8.0.1-3ubuntu2 \
    && rm -rf /var/lib/apt/lists/*

COPY agent/mcp_server.py /opt/moatbots/mcp_server.py
COPY agent/mcp-entry.sh /opt/moatbots/mcp-entry.sh
COPY share/moatbots-team/SKILL.md /opt/moatbots/SKILL.md
RUN chmod 755 /opt/moatbots/mcp-entry.sh \
    && mkdir -p /agent/shared /agents /workspace \
    && chmod 755 /agent /agents /workspace

WORKDIR /workspace
