# Build mcp-proxy on Debian (glibc). Alpine/musl venvs cannot be copied into a
# bookworm runtime that needs Oracle Instant Client for thick mode.
FROM harbor.gdalpha.com/alpha-tools/uv:python3.13-bookworm AS uv

# Prefer Aliyun PyPI for faster builds in CN environments
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

ARG UV_COMPILE_BYTECODE=1
ARG UV_LINK_MODE=copy

# Install project dependencies first (better layer cache)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --no-editable

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Runtime: Debian bookworm + latest stable Node/npm/Go
# + Instant Client 19.31 + oracle-mcp-server
# Thick mode requires glibc Instant Client (not Alpine/musl).
# ---------------------------------------------------------------------------
FROM harbor.gdalpha.com/alpha-tools/python:3.13-bookworm

# Optional CN-friendly apt mirror (comment out if not needed)
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null \
    || true

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        libaio1 \
        git \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ---- Node.js + npm: latest LTS (stable) binary, not Debian apt ----
# Prefer npmmirror for CN builds; falls back to nodejs.org.
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    case "${ARCH}" in \
        amd64) NODE_ARCH=x64 ;; \
        arm64) NODE_ARCH=arm64 ;; \
        *) echo "unsupported arch for Node.js: ${ARCH}" >&2; exit 1 ;; \
    esac; \
    NODE_INDEX_URL="https://npmmirror.com/mirrors/node/index.json"; \
    if ! curl -fsI "${NODE_INDEX_URL}" >/dev/null 2>&1; then \
        NODE_INDEX_URL="https://nodejs.org/dist/index.json"; \
    fi; \
    NODE_VERSION="$(curl -fsSL "${NODE_INDEX_URL}" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(next(x["version"].lstrip("v") for x in d if x.get("lts")))')"; \
    NODE_BASE="https://npmmirror.com/mirrors/node/v${NODE_VERSION}"; \
    if ! curl -fsI "${NODE_BASE}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.gz" >/dev/null 2>&1; then \
        NODE_BASE="https://nodejs.org/dist/v${NODE_VERSION}"; \
    fi; \
    curl -fsSL "${NODE_BASE}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.gz" \
        | tar -xz -C /usr/local --strip-components=1; \
    node -v; \
    npm -v; \
    npm config set registry https://registry.npmmirror.com; \
    npm install -g @bilims/mcp-sqlserver @fhuang/mcp-mysql-server

# ---- Go: latest stable from go.dev (CN-friendly download host) ----
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    case "${ARCH}" in \
        amd64) GO_ARCH=amd64 ;; \
        arm64) GO_ARCH=arm64 ;; \
        *) echo "unsupported arch for Go: ${ARCH}" >&2; exit 1 ;; \
    esac; \
    GO_VERSION="$(curl -fsSL https://golang.google.cn/VERSION?m=text | head -n1)"; \
    if [ -z "${GO_VERSION}" ] || [ "${GO_VERSION#go}" = "${GO_VERSION}" ]; then \
        GO_VERSION="$(curl -fsSL https://go.dev/VERSION?m=text | head -n1)"; \
    fi; \
    curl -fsSL "https://golang.google.cn/dl/${GO_VERSION}.linux-${GO_ARCH}.tar.gz" \
        -o /tmp/go.tgz \
    || curl -fsSL "https://go.dev/dl/${GO_VERSION}.linux-${GO_ARCH}.tar.gz" \
        -o /tmp/go.tgz; \
    rm -rf /usr/local/go; \
    tar -C /usr/local -xzf /tmp/go.tgz; \
    rm -f /tmp/go.tgz; \
    /usr/local/go/bin/go version

# Python / Go env (aligned with previous image behaviour)
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
    GOPROXY=https://goproxy.cn,direct \
    GOSUMDB=sum.golang.google.cn \
    GO111MODULE=on \
    GOPATH=/go \
    GOROOT=/usr/local/go \
    PATH="/usr/local/go/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# ---- Oracle Instant Client Basic 19.31 (linux x64) ----
# Prefer a local zip under mcp/app/instantclient/ for offline / reproducible builds.
# Fallback: download from Oracle OTN software (amd64). Override with:
#   docker build --build-arg INSTANTCLIENT_URL=... 
ARG INSTANTCLIENT_URL="https://download.oracle.com/otn_software/linux/instantclient/1931000/instantclient-basic-linux.x64-19.31.0.0.0dbru.zip"
# Directory name after unzip for 19.31 Basic
ENV ORACLE_CLIENT_LIB_DIR=/opt/oracle/instantclient_19_31 \
    ORACLE_HOME=/opt/oracle/instantclient_19_31 \
    LD_LIBRARY_PATH=/opt/oracle/instantclient_19_31 \
    # Default thick-ready; set THICK_MODE=0/false at runtime to force thin
    THICK_MODE=1 \
    READ_ONLY_MODE=1 \
    CACHE_DIR=/var/cache/oracle-mcp

WORKDIR /opt/oracle
# Placeholder / optional local zip(s). .dockerignore must allow this path.
COPY mcp/app/instantclient/ /tmp/instantclient-src/
RUN set -eux; \
    mkdir -p /opt/oracle; \
    ZIP="$(find /tmp/instantclient-src -maxdepth 1 -type f \( -name 'instantclient-basic-linux*.zip' -o -name '*19.31*.zip' \) | head -n 1 || true)"; \
    if [ -z "${ZIP}" ]; then \
        echo "No local Instant Client zip found; downloading 19.31 Basic..."; \
        curl -fsSL -o /tmp/instantclient.zip "${INSTANTCLIENT_URL}"; \
        ZIP=/tmp/instantclient.zip; \
    else \
        echo "Using local Instant Client zip: ${ZIP}"; \
    fi; \
    unzip -q "${ZIP}" -d /opt/oracle; \
    # Normalize path: unzip creates instantclient_19_31 (or similar)
    IC_DIR="$(find /opt/oracle -maxdepth 1 -type d -name 'instantclient_*' | head -n 1)"; \
    if [ -z "${IC_DIR}" ]; then echo "Instant Client directory not found after unzip" >&2; exit 1; fi; \
    # Ensure stable path used by ORACLE_CLIENT_LIB_DIR
    if [ "${IC_DIR}" != "/opt/oracle/instantclient_19_31" ]; then \
        rm -rf /opt/oracle/instantclient_19_31; \
        mv "${IC_DIR}" /opt/oracle/instantclient_19_31; \
    fi; \
    echo "/opt/oracle/instantclient_19_31" > /etc/ld.so.conf.d/oracle-instantclient.conf; \
    ldconfig; \
    rm -rf /tmp/instantclient-src /tmp/instantclient.zip; \
    # Smoke: client libs present
    test -e /opt/oracle/instantclient_19_31/libclntsh.so || \
      test -e /opt/oracle/instantclient_19_31/libclntsh.so.19.1

# ---- mcp-proxy venv (from build stage) ----
COPY --from=uv /app/.venv /app/.venv

# ---- Loki MCP binary ----
COPY --chmod=755 mcp/bin/loki-mcp-server /usr/local/bin/loki-mcp-server

# ---- K8S MCP binary ----
COPY --chmod=755 mcp/bin/k8s-mcp-server /usr/local/bin/k8s-mcp-server

# ---- oracle-mcp-server (UV local install, no Docker-in-Docker) ----
# Reuse uv binary from the build stage (same Harbor base), avoid extra ghcr pull
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
COPY mcp/app/oracle-mcp-server /opt/oracle-mcp-server
WORKDIR /opt/oracle-mcp-server
# Project pins .python-version=3.12; without an override uv downloads managed
# cpython-3.12 from python-build-standalone (NOT PyPI) — often very slow.
# Base image already has system Python 3.13, and requires-python is ">=3.12".
# Force system interpreter so build never fetches a remote CPython tarball.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON=3.13
RUN --mount=type=cache,target=/root/.cache/uv \
    rm -f .python-version \
    && uv sync --frozen --no-dev --python 3.13 \
    && mkdir -p /var/cache/oracle-mcp \
    && .venv/bin/python -c "import sys, oracledb; print(sys.version); print('oracledb', oracledb.__version__)"

# Runtime PATH: oracle venv + Go + mcp-proxy venv
ENV PATH="/opt/oracle-mcp-server/.venv/bin:/usr/local/go/bin:/go/bin:/app/.venv/bin:/usr/local/bin:${PATH}" \
    ORACLE_CLIENT_LIB_DIR=/opt/oracle/instantclient_19_31 \
    PYTHONPATH=/opt/oracle-mcp-server

WORKDIR /app

# Default entrypoint remains mcp-proxy; named servers (oracle/loki/...) come from servers.json
# tini reaps zombies from stdio child MCP processes (replaces alpine catatonit)
ENTRYPOINT ["tini", "--", "mcp-proxy"]