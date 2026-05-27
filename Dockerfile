# AgentFlow daemon — legacy root Dockerfile.
#
# As of feat/daemon-image-split (PR-B in the hosted-device image-split stack)
# this file is no longer built by CI. The image graph now lives under docker/:
#
#   docker/Dockerfile.base       → ghcr.io/lnlockly/agentflow-daemon-base
#   docker/Dockerfile.coder      → ghcr.io/lnlockly/agentflow-daemon-coder
#   docker/Dockerfile.assistant  → ghcr.io/lnlockly/agentflow-daemon-assistant
#   docker/Dockerfile.universal  → ghcr.io/lnlockly/agentflow-daemon-universal
#
# The universal variant is also published under the legacy tag
# ghcr.io/lnlockly/agentflow-daemon:<sha> so existing manifests in
# agentflow-agents/src/routes/me-hosted-devices.ts keep pulling.
#
# For local builds, pick the variant directly, eg:
#   docker build -f docker/Dockerfile.base       -t af-daemon-base .
#   docker build -f docker/Dockerfile.universal  -t af-daemon-universal \
#     --build-arg BASE_IMAGE=af-daemon-base .

FROM ghcr.io/lnlockly/agentflow-daemon-universal:latest
