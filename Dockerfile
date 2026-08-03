FROM python:3.11-slim

WORKDIR /app

# curl is required by the compose healthcheck; add other system packages here (e.g. cups-client, libmagic).
# gosu is used by the entrypoint to drop privileges from root to the app user.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gosu \
    && rm -rf /var/lib/apt/lists/*

# Fixed uid keeps volume ownership stable across image rebuilds.
RUN useradd -m -u 1000 app

# Dependencies as a separate layer: change less often than code → cached better
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime state directory. When a named volume is first initialised from this
# image, docker copies the ownership of this dir — so the volume starts owned by app.
RUN mkdir -p data && chown app:app data

# Code and static assets
COPY src/ src/
COPY templates/ templates/
COPY main.py .
# --chmod pins the executable bit: exec-form ENTRYPOINT fails with "permission
# denied" if the bit is lost in the build context (Windows checkout, tar copy).
COPY --chmod=0755 entrypoint.sh /entrypoint.sh

# Build identity: WHICH revision this image contains. There is no git and no repository
# inside the container, so this is the only moment it can be established — CI passes
# `--build-arg BUILD_REVISION=${{ github.sha }}` (.github/workflows/ghcr-check-publish.yml)
# and the ENV bakes it in for the running process to report on /healthz and /admin.
# Declared LAST on purpose: the value changes on every commit, and anything below an ARG
# that changes is a cache miss — here there is nothing below it but metadata.
# The `unknown` default keeps a plain `docker build .` working; src/settings.py turns both
# an unset and an empty value into the same honest "unknown".
ARG BUILD_REVISION=unknown
ENV BUILD_REVISION=${BUILD_REVISION}
# The standard OCI label, so `docker inspect` / `docker image ls --format` answer the same
# question without an HTTP request — useful when the container will not start at all.
LABEL org.opencontainers.image.revision="${BUILD_REVISION}"

# No EXPOSE: the service is published by Traefik via docker-compose labels.

# No USER directive on purpose: the entrypoint starts as root, heals /app/data
# ownership (migration from older root-based images) and drops to app via gosu.
# A compose `user:` override is respected (the entrypoint then just execs).
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py"]
