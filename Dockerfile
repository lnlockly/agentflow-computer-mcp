# AgentFlow desktop daemon — hosted image.
#
# This is what runs inside a Linux pod when a user picks "облачное
# устройство" → kind=daemon in the AgentFlow cabinet. It exposes the same
# tool surface the Mac/Win/Linux installer provides, but the pod has no
# physical screen — Xvfb stands in for one so headed Chromium and
# Playwright work, and screen_record_* writes a real .webm through the
# Playwright recordVideo API (no host Screen Recording permission needed).
#
# Image layout:
#   /opt/agentflow         — installed daemon (editable install)
#   /data                  — persistent volume (workspaces, recordings)
#   /etc/agentflow         — daemon config seeded from secrets
#
# Entrypoint launches:
#   1. Xvfb on :99
#   2. agentflow-desktop run --headless
#
# Build:
#   docker build -t ghcr.io/lnlockly/agentflow-daemon:latest .
#
# Run locally (one-off, for testing):
#   docker run --rm -it -e AF_API_KEY=... -e AF_DEVICE_TOKEN=... \
#     ghcr.io/lnlockly/agentflow-daemon:latest

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    AF_HOSTED_KIND=daemon

# System packages:
#   - xvfb + xauth — virtual display so headed Chromium works
#   - chromium, fonts — browsers + Cyrillic/emoji rendering
#   - xdotool, wmctrl — window control surface for activate_app on Linux
#   - x11-utils, x11-xserver-utils — xdpyinfo / xset (sanity probes)
#   - ffmpeg — optional, used by screen_record fallback path
#   - git, curl, ca-certificates, jq — daemon-side tooling
#   - tini — pid 1 reaper so xvfb child processes don't zombie
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        xauth \
        x11-utils \
        x11-xserver-utils \
        xdotool \
        wmctrl \
        chromium \
        chromium-driver \
        fluxbox \
        fonts-dejavu \
        fonts-noto-color-emoji \
        fonts-noto-cjk \
        ffmpeg \
        git \
        curl \
        ca-certificates \
        jq \
        tini \
        procps \
        sudo \
        x11vnc \
        novnc \
        websockify \
        python3-websockify \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Pod's default uid is 0 in many vclusters but the daemon
# only writes to /data and ~/.agentflow so we don't need root after the
# image is built.
RUN useradd -ms /bin/bash -u 1001 agentflow \
    && mkdir -p /data /etc/agentflow /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R agentflow:agentflow /data /etc/agentflow

WORKDIR /opt/agentflow

# Install Python deps first for caching. Copy only the lockable bits.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install -e ".[linux]"

# Playwright Chromium runtime — installs into ~/.cache. Use system
# chromium-driver instead when possible, but bundle the playwright pin
# so .recordVideo works on first task.
RUN pip install "playwright>=1.40" \
    && python -m playwright install chromium

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
# Port 6080 — noVNC web client (browser opens it in iframe от cabinet).
# Port 5900 — raw VNC if кому-то нужен прямой клиент.
EXPOSE 8765 6080 5900

HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8765/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agentflow-entrypoint"]
