# The virtualenv is built with uv, and only it is copied into the runtime
# image, the official Python one without build tools.
ARG PYTHON_VERSION=3.12
ARG PYTHON_DIGEST=sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG UV_VERSION
ARG UV_DIGEST=sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_DIGEST} AS uv

FROM python:${PYTHON_VERSION}-slim@${PYTHON_DIGEST} AS builder

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/afm

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY alternative_fabric_manager ./alternative_fabric_manager
RUN uv sync --locked --no-dev --no-editable

FROM python:${PYTHON_VERSION}-slim@${PYTHON_DIGEST}

LABEL org.opencontainers.image.title="alternative-fabric-manager" \
      org.opencontainers.image.description="An InfiniBand fabric manager for networking-mlnx that drives OpenSM partitions" \
      org.opencontainers.image.source="https://github.com/stackhpc/alternative-fabric-manager" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=builder /opt/afm /opt/afm
ENV PATH=/opt/afm/bin:$PATH \
    PYTHONUNBUFFERED=1

# Runs as root: it writes OpenSM's partitions file, and signals the OpenSM
# container through the Docker socket, which is root-equivalent anyway.
VOLUME ["/var/lib/alternative-fabric-manager"]
EXPOSE 8090
ENTRYPOINT ["alternative-fabric-manager"]
CMD ["--config-file", "/etc/alternative-fabric-manager/afm.conf"]
