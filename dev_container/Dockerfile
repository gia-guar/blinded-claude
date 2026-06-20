# Dev container: runs Claude Code. Has NO data volume mounted — only source.
# Claude Code is installed at build time (build has internet); at runtime the
# container reaches the Anthropic API over the `external` network and the MCP
# server over the internal `mcp_bridge` network.
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /workspace
CMD ["sleep", "infinity"]
