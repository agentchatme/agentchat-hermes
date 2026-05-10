#!/bin/bash
# Hermes Agent test-VM bootstrap — runs as root via GCE startup-script.
#
# Goal: leave the VM with Hermes Agent installed and direct root SSH
# enabled, so the operator can `ssh root@<ip>` and run `hermes setup`,
# `pip install agentchatme-hermes`, `hermes agentchat register`, etc.,
# without further setup.
#
# Mirrors the agentchat-openclaw test-bot bootstrap pattern: bare-minimum
# runtime install + SSH key, no plugin config, no channel registration,
# no secrets. The operator drives all of that.

set -e
exec > /var/log/hermes-bootstrap.log 2>&1
echo "=== $(date -u) bootstrap start ==="

# 1. Disable IPv6 BEFORE anything hits the network.
#    GCE IPv6 path to api.openrouter.ai / api.agentchat.me stalls 30s+
#    before falling back. Off across the board. Same as OpenClaw bots.
cat > /etc/sysctl.d/99-disable-ipv6.conf <<EOF
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
sysctl --system >/dev/null

# 2. System packages. Hermes installer pulls uv + Python 3.11 + Node +
#    ripgrep + ffmpeg itself, but it depends on a working curl + git +
#    ca-certificates baseline. ffmpeg is also pulled by the installer
#    but pre-staging it via apt is faster than uv's install path.
apt-get update -y
apt-get install -y curl ca-certificates gnupg git build-essential ffmpeg ripgrep

# 3. Install Hermes Agent (canonical one-liner from the README).
#
# Critical: the installer's first step is to install `uv` into
# ~/.local/bin/uv. The installer then immediately tries to USE uv
# from PATH and bails with "uv installed but not found on PATH" if
# ~/.local/bin isn't already there at startup. So we pre-stamp it
# into PATH for THIS shell before invoking the installer. The
# installer's own .bashrc edits handle subsequent shells; this line
# just fixes the chicken-and-egg for the bootstrap-time first run.
#
# `|| true` swallows any expected non-zero exit (e.g. the installer's
# interactive setup prompt failing on /dev/tty in non-interactive
# mode — the CLI itself is installed regardless).
export PATH="/root/.local/bin:$PATH"
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash || true

# 4. The Hermes installer drops the shim at /usr/local/bin/hermes
#    directly (root install on Linux uses FHS layout — /usr/local/lib
#    for code, /usr/local/bin for the shim). No symlink needed; the
#    installer announces "'hermes' was linked into /usr/local/bin and
#    is ready to use" at the end of a successful run.
#
# Verify hermes resolves so the bootstrap log can be grepped for a
# clean install signal:
which hermes || echo "WARNING: hermes not found on PATH after install"
hermes --version 2>&1 || echo "WARNING: hermes --version failed"

# Ensure ~/.local/bin is in PATH for interactive root logins (the
# installer normally appends to .bashrc, but we re-stamp here so the
# symlink and the PATH-based discovery both work).
if ! grep -q 'HOME/.local/bin' /root/.bashrc 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> /root/.bashrc
fi

# 5. Enable direct root SSH with the operator's gcloud-managed key.
#    Same key as the OpenClaw test bots — one SSH credential covers
#    every test VM in this project.
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'PUBKEY'
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC+r0sf5+wWQYEycQw4tE/CgCawK603ctIxD4o4laWgnd+twmaGyMt73R2nDUBka6QWdl+9pdWkEA4oa5JOzNMPPGpBfjuxLDWS3NWCKyYEhGAhsvJxA6GxxG9hv0TSrrIwXfDyAD3f9VfRLjY8Wr4Riu5B4nOvRtJVP7c75ERz1NjYC3/W3pAzeNNs9YhNyzV861rAqYfyNfXESKSCeCIMPOQ9wdv2ibS5iP3+Ed7xWrQfB8ncQozKvbpF6dAdgmTYjsAtJjffhpmbgmBmA8C5eGaluDpe41aHiqev5byrrXwQADq25ExqOlS8eKHLjdmY22qj3vFYwCPVcBRhyK+/ sani-gcloud
PUBKEY
chmod 600 /root/.ssh/authorized_keys

# Permit root SSH (Debian 12 default is `prohibit-password` which would
# block our key-based root login without this).
sed -i 's/^#*PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl restart ssh

# 6. Marker
mkdir -p /var/lib/hermes
date -u > /var/lib/hermes/bootstrap.done
echo "=== $(date -u) bootstrap done ==="
