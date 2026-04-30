# Strata - Reporting system
# Multi-stage build for minimal image size

FROM python:3.14-slim AS builder

# Install build dependencies. git is needed for `uv pip install git+...`;
# libldap2-dev / libsasl2-dev are needed to build python-ldap (gatekeeper dep).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    libldap2-dev \
    libsasl2-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy project files needed for install
COPY pyproject.toml ./
COPY src/ ./src/

# Create virtual environment and install dependencies.
# gatekeeper and outbox are pulled from git; --no-sources makes uv
# ignore the [tool.uv.sources] editable-path overrides in pyproject.toml,
# which point at sibling checkouts that don't exist inside the image.
RUN uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv pip install git+https://github.com/technobok/gatekeeper.git && \
    uv pip install git+https://github.com/technobok/outbox.git && \
    uv pip install --no-sources .

# Production image
FROM python:3.14-slim

# Runtime libs for python-ldap (gatekeeper dep).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libldap-common \
    libsasl2-2 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash strata

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY --chown=strata:strata src/ ./src/
COPY --chown=strata:strata worker/ ./worker/
COPY --chown=strata:strata database/ ./database/
COPY --chown=strata:strata wsgi.py ./
COPY --chown=strata:strata pyproject.toml ./

# Create instance directory
RUN mkdir -p /app/instance/cache && chown -R strata:strata /app/instance

# Set environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

USER strata

# Default port; override at runtime with the PORT env var.
EXPOSE 5000

# Default command runs the Flask app
CMD ["python", "wsgi.py"]
