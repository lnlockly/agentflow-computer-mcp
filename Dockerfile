# AgentFlow desktop daemon — hosted image.
#
# Default build is SLIM (~400 MB): Python + Node + opencode-ai + tini.
# Project work via opencode runs in this. No browser, no virtual display.
#
# Optional addons via build args:
#   --build-arg INSTALL_BROWSER=1   adds Xvfb, Chromium, Playwright, fonts,
#                                   ffmpeg, xdotool/wmctrl (~700 MB).
#   --build-arg INSTALL_VNC=1       adds x11vnc + noVNC + fluxbox so the
#                                   cabinet /live page can stream pod
#                                   screen via wss (~70 MB). Requires
#                                   INSTALL_BROWSER=1.
#
# Or at runtime, owner runs `agentflow-install-addon browser` /
# `agentflow-install-addon vnc` inside the pod — same packages, applied
# on-demand without rebuilding the image.
#
# Image layout:
#   /opt/agentflow         — installed daemon (editable install)
#   /data                  — persistent volume (workspaces, recordings)
#   /etc/agentflow         — daemon config seeded from secrets
#
# Build (default slim):
#   docker build -t ghcr.io/lnlockly/agentflow-daemon:latest .
# Build full (with browser + VNC, like the old image):
#   docker build --build-arg INSTALL_BROWSER=1 --build-arg INSTALL_VNC=1 \
#                -t ghcr.io/lnlockly/agentflow-daemon:full .

FROM python:3.12-slim-bookworm AS base

ARG INSTALL_BROWSER=0
ARG INSTALL_VNC=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    AF_HOSTED_KIND=daemon

# Core packages required by every daemon profile:
#   - git, curl, ca-certificates, jq — daemon-side tooling
#   - tini — pid 1 reaper
#   - nodejs + npm + pnpm + yarn — for opencode and project package managers
#   - opencode-ai (npm global) — the default project runner
#   - sudo, procps — daemon's runtime helpers
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        jq \
        tini \
        procps \
        sudo \
        nodejs \
        npm \
    # pnpm 10.x requires Node 22.13+; Debian bookworm ships Node 18.20.
    && npm install -g --no-audit --no-fund pnpm@9 yarn opencode-ai \
    && rm -rf /var/lib/apt/lists/*

# Optional browser stack — Xvfb + Chromium + Playwright + fonts + ffmpeg.
# Required for kwork money loop, autonomous browser tasks, screen-record.
# NOT required for opencode-driven project work.
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            xvfb xauth x11-utils x11-xserver-utils xdotool wmctrl \
            chromium chromium-driver \
            fonts-dejavu fonts-noto-color-emoji fonts-noto-cjk \
            ffmpeg \
        && rm -rf /var/lib/apt/lists/* \
        && pip install "playwright>=1.40" \
        && python -m playwright install chromium; \
    fi

# Optional VNC stack — exposes pod screen at ws://:6080/ for cabinet /live.
RUN if [ "$INSTALL_VNC" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            x11vnc novnc websockify python3-websockify fluxbox \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Runtime addon installer — owner can `kubectl exec` and run this to add
# browser/vnc support to an already-running slim pod without a rebuild.
COPY docker/install-addon.sh /usr/local/bin/agentflow-install-addon
RUN chmod +x /usr/local/bin/agentflow-install-addon

# Non-root user. Pod's default uid is 0 in many vclusters but the daemon
# only writes to /data, ~/.agentflow, and /workspace so we don't need
# root after the image is built.
RUN useradd -ms /bin/bash -u 1001 agentflow \
    && mkdir -p /data /etc/agentflow /tmp/.X11-unix /workspace \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R agentflow:agentflow /data /etc/agentflow /workspace \
    # NOPASSWD on apt + chmod so install-addon.sh works from the daemon
    # user without an interactive sudo prompt.
    && echo 'agentflow ALL=(root) NOPASSWD: /usr/bin/apt-get, /bin/chmod, /usr/bin/python3' \
       > /etc/sudoers.d/90-agentflow-addons \
    && chmod 0440 /etc/sudoers.d/90-agentflow-addons

WORKDIR /opt/agentflow

# Install Python deps first for caching. Copy only the lockable bits.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install -e ".[linux]"

# Copy entrypoint last so editing it doesn't bust the deps layer.
COPY docker/entrypoint.sh /usr/local/bin/agentflow-entrypoint
RUN chmod +x /usr/local/bin/agentflow-entrypoint

USER agentflow
WORKDIR /data

# AF_HOSTED=1 signals the Python config layer (load_scope) that there's no
# user to dismiss native confirm dialogs — every confirm() defaults to
# allow, the cabinet remains the only authority via /me/devices/:id/scope.
ENV AF_HOSTED=1

# Healthcheck — daemon HTTP listener on :8765 (control plane), used by
# the hosted-device reconciler to mark the device Ready.
# Port 6080 — noVNC web client (only when INSTALL_VNC=1).
# Port 5900 — raw VNC (only when INSTALL_VNC=1).
EXPOSE 8765 6080 5900

HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8765/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agentflow-entrypoint"]
