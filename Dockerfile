# Hermes reviewer image for ReviewBench (AGENT_CONTRACT.md).
# Credential (secrets.env): NVIDIA_API_KEY  - never baked into the image.
# Egress: integrate.api.nvidia.com (from the registered model API URL).
# Configuration (RB_CONFIG_*): RB_CONFIG_MODEL selects the NIM model id.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir hermes-agent==0.19.0

WORKDIR /agent
COPY agent.py ./

ENV HOME=/tmp/hermes-home
RUN mkdir -p /tmp/hermes-home

ENTRYPOINT ["python", "/agent/agent.py"]
