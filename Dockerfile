# Build stage with explicit platform specification
FROM ghcr.io/astral-sh/uv:python3.13-alpine AS uv

# Use Aliyun mirrors for Python and Alpine package downloads
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/

# Install the project into /app
WORKDIR /app

# Enable bytecode compilation
ARG UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ARG UV_LINK_MODE=copy

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --no-editable

# Then, add the rest of the project source code and install it
# Installing separately from its dependencies allows optimal layer caching
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

RUN sed -i 's|https://dl-cdn.alpinelinux.org/alpine|https://mirrors.aliyun.com/alpine|g' /etc/apk/repositories
RUN apk add --update --no-cache catatonit

# Final stage with explicit platform specification
FROM python:3.13-alpine

# Use the Aliyun Alpine mirror and install the Node.js runtime.
# Configure npm to use the Aliyun registry for package installs (npx/npm).
RUN sed -i 's|https://dl-cdn.alpinelinux.org/alpine|https://mirrors.aliyun.com/alpine|g' /etc/apk/repositories \
    && apk add --no-cache nodejs npm \
    && npm config set registry https://registry.npmmirror.com

# Keep Python package downloads on the Aliyun mirror in the runtime image too
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/

COPY --from=uv --chown=app:app /app/.venv /app/.venv
COPY --from=uv /usr/bin/catatonit /usr/bin/
COPY --from=uv /usr/libexec/podman/catatonit /usr/libexec/podman/

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["catatonit", "--", "mcp-proxy"]
