# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm
ARG NODE_IMAGE=node:24.13.0-bookworm-slim

FROM ${NODE_IMAGE} AS codex-cli
ARG CODEX_VERSION=0.146.0
RUN npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && codex --version

FROM ${PYTHON_IMAGE} AS builder
ARG UV_VERSION=0.11.6
COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION} /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ARG SOURCE_SHA=unknown
LABEL org.opencontainers.image.title="agentd" \
      org.opencontainers.image.description="Hardened local agent control plane" \
      org.opencontainers.image.revision="${SOURCE_SHA}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bubblewrap \
        ca-certificates \
        git \
        libstdc++6 \
        uidmap \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 bened \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/bened \
        --shell /usr/sbin/nologin bened \
    && install -d -o 1000 -g 1000 -m 0700 \
        /home/bened/.cache/uv \
        /home/bened/.local/state/agentd \
        /home/bened/.local/share/agentd/workspaces \
        /home/bened/.local/share/agentd/codex-home

COPY --from=builder /build/.venv /opt/agentd/venv
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/bin/codex /usr/local/bin/codex
COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --chmod=0755 deploy/container/bwrap /usr/local/libexec/agentd/bwrap
COPY --chmod=0555 \
    deploy/security/runtime_sandbox_probe.py \
    deploy/security/sandbox_payload.py \
    /opt/agentd/security/
COPY --chmod=0444 deploy/container/config.toml /opt/agentd/security/config.toml

ENV PATH="/usr/local/libexec/agentd:/opt/agentd/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="/home/bened" \
    CODEX_HOME="/home/bened/.local/share/agentd/codex-home" \
    UV_CACHE_DIR="/home/bened/.cache/uv" \
    XDG_CACHE_HOME="/tmp/agentd-cache" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

USER 1000:1000
WORKDIR /home/bened/goldenage

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/opt/agentd/venv/bin/python", "-c", "import os; os.kill(1, 0); import agentd"]

ENTRYPOINT ["/opt/agentd/venv/bin/agentd"]
CMD ["serve"]
