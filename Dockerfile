# The virtualenv is built with uv, and only it is copied into the runtime
# image, the official Python one without build tools.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/afm

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY alternative_fabric_manager ./alternative_fabric_manager
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim

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
