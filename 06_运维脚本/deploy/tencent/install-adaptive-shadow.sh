#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" != "0" ]]; then
    echo "Run with sudo: sudo bash /opt/quant/deploy/tencent/install-adaptive-shadow.sh" >&2
    exit 2
fi
if [[ ! -x /opt/quant-venv/bin/quant || ! -f /opt/quant/pyproject.toml ]]; then
    echo "Install the application and crypto dependencies under /opt/quant first." >&2
    exit 2
fi
if ! id quant-adaptive >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/quant-adaptive --shell /usr/sbin/nologin quant-adaptive
fi
install -d -o quant-adaptive -g quant-adaptive -m 0700 /var/lib/quant-adaptive
install -o root -g root -m 0644 /opt/quant/deploy/tencent/quant-adaptive-shadow.service /etc/systemd/system/quant-adaptive-shadow.service
install -o root -g root -m 0644 /opt/quant/deploy/tencent/quant-adaptive-testnet.service /etc/systemd/system/quant-adaptive-testnet.service
install -o root -g root -m 0644 /opt/quant/deploy/tencent/quant-alert@.service /etc/systemd/system/quant-alert@.service
systemctl daemon-reload
echo "Installed, not started. Configure /etc/quant-ai.env (root:root mode 0600), then stop the old quant-testnet order service before enabling adaptive shadow."
echo "Shadow: sudo systemctl disable --now quant-testnet && sudo systemctl enable --now quant-adaptive-shadow"
echo "The Testnet auto unit must remain disabled until the 30-day and acceptance gates pass."
