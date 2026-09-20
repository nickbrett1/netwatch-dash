#!/bin/bash
# This file is executed once per session to set up the devcontainer.
# For example:
# echo "Running devcontainer setup script..."
# npm install

CURRENT_USER=$(whoami)
USER_HOME_DIR="$HOME"

echo "INFO: Restoring or backing up SSH host keys..."
sudo mkdir -p /var/lib/tailscale/ssh
if [ -n "$(ls -A /var/lib/tailscale/ssh/ssh_host_* 2>/dev/null)" ]; then
    echo "INFO: Restoring SSH host keys from /var/lib/tailscale/ssh..."
    sudo cp -f /var/lib/tailscale/ssh/ssh_host_* /etc/ssh/
    sudo chmod 600 /etc/ssh/ssh_host_*_key
    sudo chmod 644 /etc/ssh/ssh_host_*_key.pub 2>/dev/null || true
else
    echo "INFO: Backing up SSH host keys to /var/lib/tailscale/ssh..."
    sudo ssh-keygen -A || true
    sudo cp -f /etc/ssh/ssh_host_* /var/lib/tailscale/ssh/
fi

echo "INFO: Ensuring SSH service is running..."
sudo service ssh restart



echo "INFO: Creating Oh My Zsh custom directories..."
mkdir -p "$USER_HOME_DIR/.oh-my-zsh/custom/themes" "$USER_HOME_DIR/.oh-my-zsh/custom/plugins"

if [ -f "/workspaces/netwatch-dash/.devcontainer/.zshrc" ]; then
    echo "INFO: Copying .zshrc to $USER_HOME_DIR/.zshrc"
    cp "/workspaces/netwatch-dash/.devcontainer/.zshrc" "$USER_HOME_DIR/.zshrc"
    sudo chown "$CURRENT_USER:$CURRENT_USER" "$USER_HOME_DIR/.zshrc"
else
    echo "INFO: /workspaces/netwatch-dash/.devcontainer/.zshrc not found, skipping copy."
fi

if [ -f "/workspaces/netwatch-dash/.devcontainer/.p10k.zsh" ]; then
    echo "INFO: Copying .p10k.zsh to $USER_HOME_DIR/.p10k.zsh"
    cp "/workspaces/netwatch-dash/.devcontainer/.p10k.zsh" "$USER_HOME_DIR/.p10k.zsh"
    sudo chown "$CURRENT_USER:$CURRENT_USER" "$USER_HOME_DIR/.p10k.zsh"
else
    echo "INFO: /workspaces/netwatch-dash/.devcontainer/.p10k.zsh not found, skipping copy."
fi

if [ -f "/workspaces/netwatch-dash/.devcontainer/.tmux.conf" ]; then
    echo "INFO: Copying .tmux.conf to $USER_HOME_DIR/.tmux.conf"
    cp "/workspaces/netwatch-dash/.devcontainer/.tmux.conf" "$USER_HOME_DIR/.tmux.conf"
    sudo chown "$CURRENT_USER:$CURRENT_USER" "$USER_HOME_DIR/.tmux.conf"
else
    echo "INFO: /workspaces/netwatch-dash/.devcontainer/.tmux.conf not found, skipping copy."
fi




echo "INFO: Installing Cursor CLI..."
curl https://cursor.com/install -fsS | bash



# Setup python virtual environment and install dependencies
# (memo: genproj python devcontainer .venv PATH). postCreate runs with the
# workspace as CWD, but cd explicitly so this also works when invoked from
# elsewhere (e.g. a manual re-run after the container restarted in $HOME).
cd "/workspaces/netwatch-dash" 2>/dev/null || true

if [ ! -d ".venv" ]; then
    echo "INFO: Creating Python virtual environment (.venv)..."
    python3 -m venv .venv
fi

if [ -f "requirements.txt" ]; then
    echo "INFO: Installing dependencies from requirements.txt..."
    .venv/bin/pip install -r requirements.txt
elif [ -f "pyproject.toml" ]; then
    echo "INFO: Installing dependencies from pyproject.toml (dev extras)..."
    .venv/bin/pip install -e ".[dev]"
fi

# genproj-python-venv-path: expose .venv/bin on PATH for shells that do NOT
# inherit devcontainer.json remoteEnv (VS Code terminals get PATH from
# remoteEnv; ssh / 'bash -lc' / tmux panes started outside VS Code do not).
# The marker comment keeps this idempotent across post-create re-runs.
VENV_RC_MARKER='# genproj-python-venv-path'
if ! grep -qF "$VENV_RC_MARKER" "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" <<'EOF'
# genproj-python-venv-path: prefer project .venv
if [ -d "/workspaces/netwatch-dash/.venv/bin" ]; then
    export PATH="/workspaces/netwatch-dash/.venv/bin:$PATH"
fi
EOF
    echo "INFO: Added .venv PATH hook to ~/.bashrc"
fi
if ! grep -qF "$VENV_RC_MARKER" "$HOME/.zshrc" 2>/dev/null; then
    cat >> "$HOME/.zshrc" <<'EOF'
# genproj-python-venv-path: prefer project .venv
if [ -d "/workspaces/netwatch-dash/.venv/bin" ]; then
    export PATH="/workspaces/netwatch-dash/.venv/bin:$PATH"
fi
EOF
    echo "INFO: Added .venv PATH hook to ~/.zshrc"
fi







echo "INFO: Configuring git safe directory..."
git config --global --add safe.directory /workspaces/netwatch-dash



echo "INFO: Installing git pre-commit hooks (lint-staged)..."
(cd /workspaces/netwatch-dash && npx --yes simple-git-hooks) || echo "WARN: Run 'npx simple-git-hooks' to install hooks manually."









echo "INFO: Checking Tailscale status..."
if ! command -v tailscale &> /dev/null; then
    echo "INFO: Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
fi

if ! pgrep -x tailscaled > /dev/null; then
    echo "INFO: Starting Tailscale daemon..."
    sudo start-stop-daemon --start --background --oknodo --pidfile /var/run/tailscaled.pid --make-pidfile --exec /usr/sbin/tailscaled -- --state=/var/lib/tailscale/tailscaled.state
fi

echo "INFO: Custom container setup script finished."
