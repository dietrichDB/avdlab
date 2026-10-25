# avdlab-agent: runs on the lab host next to the cEOS containers (deploy / ANTA NRFU / ZTP).
#   docker build -t avdlab/agent:6.4.0 agent/
# then point AVDLAB_AGENT_IMAGE at the pushed tag (default in settings is the upstream universal image).
ARG BASE_IMAGE=ghcr.io/aristanetworks/avd/universal:python3.11-avd-v6.4.0
FROM ${BASE_IMAGE}

ARG ANTA_VERSION=1.10.0
ARG EOS_DOWNLOADER_SPEC=">=0.13"

USER root
# ZTP/DHCP (dnsmasq), netns/link tooling, sync + JSON helpers, ssh client for host-local hops
RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
         dnsmasq iproute2 curl jq rsync openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ANTA pinned (the universal image may already ship it) + eos-downloader (ardl)
RUN set -eux; \
    if python3 -c "import importlib.metadata as m, sys; sys.exit(0 if m.version('anta') == '${ANTA_VERSION}' else 1)" 2>/dev/null; then \
      echo "anta ${ANTA_VERSION} already present"; \
    else \
      python3 -m pip install --no-cache-dir "anta[cli]==${ANTA_VERSION}"; \
    fi; \
    python3 -m pip install --no-cache-dir "eos-downloader${EOS_DOWNLOADER_SPEC}"; \
    python3 -c "import pyavd, anta; print('pyavd', pyavd.__version__, 'anta', anta.__version__)"; \
    ardl --version || true

LABEL org.opencontainers.image.title="avdlab-agent" \
      org.opencontainers.image.description="AVD universal image + ANTA ${ANTA_VERSION} + eos-downloader + dnsmasq for avdlab lab hosts"

# back to the image's unprivileged user (the universal image uses 'avd'); dnsmasq jobs run with --user 0 explicitly
USER avd
WORKDIR /home/avd
