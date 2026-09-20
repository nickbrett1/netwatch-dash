#!/bin/bash
set -e

# Determine the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The project root directory is one level up from the scripts directory
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Change to the project root directory so that relative paths work correctly
cd "$PROJECT_ROOT"

# Doppler login first: it is the critical path for goose (and the wrangler /
# google-cloud sections below depend on it). Tailscale is optional SSH access
# and its interactive 'tailscale up' prompt must not block the rest.


# Tailscale login
if command -v tailscale &> /dev/null; then
  if ! pgrep -x tailscaled > /dev/null; then
    echo "INFO: Starting Tailscale daemon..."
    sudo tailscaled --state=/var/lib/tailscale/tailscaled.state > /dev/null 2>&1 &
    sleep 2
  fi
  if ! sudo tailscale status &> /dev/null; then
    echo "INFO: Logging into Tailscale..."
    sudo tailscale up --hostname=netwatch-dash
  else
    echo "✅ Already logged in to Tailscale."
  fi
fi







echo "Cloud login script finished."
