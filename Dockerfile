# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm
ARG NODE_IMAGE=node:24.13.0-bookworm-slim
ARG UV_VERSION=0.11.6

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-runtime

FROM ${NODE_IMAGE} AS codex-cli
ARG CODEX_VERSION=0.146.0
RUN npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && codex --version

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv-runtime /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS bwrap-compat-builder
RUN apt-get update \
    && apt-get install --yes --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/container/bwrap_compat.c /build/bwrap_compat.c
RUN cc \
        -std=c17 \
        -O2 \
        -D_FORTIFY_SOURCE=2 \
        -fPIE \
        -pie \
        -Wall \
        -Wextra \
        -Werror \
        -Wl,-z,relro,-z,now \
        -s \
        -o /build/bwrap \
        /build/bwrap_compat.c

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
    && install -d -o 0 -g 0 -m 0555 /usr/libexec/agentd \
    && mv /usr/bin/bwrap /usr/libexec/agentd/bwrap.real \
    && chown 0:0 /usr/libexec/agentd/bwrap.real \
    && chmod 0555 /usr/libexec/agentd/bwrap.real \
    && install -d -o 1000 -g 1000 -m 0700 \
        /home/bened/.cache/uv \
        /home/bened/.local/state/agentd \
        /home/bened/.local/share/agentd/workspaces \
        /home/bened/.local/share/agentd/codex-home

COPY --from=bwrap-compat-builder --chown=0:0 --chmod=0555 \
    /build/bwrap /usr/bin/bwrap
COPY --from=builder /build/.venv /opt/agentd/venv
RUN ln -s \
        /opt/agentd/venv/lib/python3.12/site-packages/codex_cli_bin/bin/codex \
        /usr/libexec/agentd/codex-linux-sandbox \
    && ln -s \
        /opt/agentd/venv/lib/python3.12/site-packages/codex_cli_bin/bin/codex \
        /usr/libexec/agentd/codex-execve-wrapper \
    && ln -s \
        /opt/agentd/venv/lib/python3.12/site-packages/codex_cli_bin/bin/codex \
        /usr/libexec/agentd/apply_patch \
    && ln -s \
        /opt/agentd/venv/lib/python3.12/site-packages/codex_cli_bin/bin/codex \
        /usr/libexec/agentd/applypatch
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/bin/codex /usr/local/bin/codex
COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --chmod=0555 \
    deploy/security/runtime_sandbox_probe.py \
    deploy/security/sandbox_payload.py \
    /opt/agentd/security/
COPY --chmod=0444 deploy/container/config.toml /opt/agentd/security/config.toml

ENV PATH="/opt/agentd/venv/bin:/usr/libexec/agentd:/usr/local/bin:/usr/bin:/bin" \
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
