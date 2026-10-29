#!/usr/bin/env bash
# Run on a fresh Ubuntu 24.04 Tencent Cloud server after extraction to /opt/quant.
set -euo pipefail
if [[ "$(id -u)" != "0" ]]; then
    echo "Run with sudo: sudo bash /opt/quant/deploy/tencent/install-testnet.sh" >&2
    exit 2
fi
source /etc/os-release
if [[ "$ID" != "ubuntu" || "$VERSION_ID" != "24.04" ]]; then
    echo "This installer targets Ubuntu 24.04 (Python 3.12)." >&2
    exit 2
fi
if [[ ! -f /opt/quant/pyproject.toml ]]; then
    echo "Extract the deployment archive to /opt/quant first." >&2
    exit 2
fi
apt-get update
apt-get install -y python3.12 python3.12-venv ca-certificates
if ! id quant-testnet >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/quant-testnet --shell /usr/sbin/nologin quant-testnet
fi
install -d -o quant-testnet -g quant-testnet -m 0700 /var/lib/quant-testnet
python3.12 -m venv /opt/quant-venv
/opt/quant-venv/bin/python -m pip install --upgrade pip
/opt/quant-venv/bin/python -m pip install --no-cache-dir -e '/opt/quant[crypto]'
install -o root -g root -m 0644 /opt/quant/deploy/tencent/quant-testnet.service /etc/systemd/system/quant-testnet.service
systemctl daemon-reload
echo "Installed, not started. Add separate Spot Testnet credentials to /etc/quant-testnet.env (root:root, mode 0600)."
echo "Then preflight: sudo -u quant-testnet timeout 45s /opt/quant-venv/bin/python /opt/quant/scripts/testnet_market_check.py"
echo "Start only after all four checks pass: sudo systemctl enable --now quant-testnet"
