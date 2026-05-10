#!/bin/bash
# One-shot remote Hermes installer. Runs as root on the VM.
# Sourced by hermes-pilot-bootstrap.sh and re-runnable for recovery.
set -e
exec > /var/log/hermes-install.log 2>&1
export PATH="/root/.local/bin:$PATH"
echo "=== $(date -u) hermes install start ==="
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
echo "=== $(date -u) hermes install done ==="
which hermes || true
hermes --version 2>&1 || true
echo "=== $(date -u) hermes install verify done ==="
