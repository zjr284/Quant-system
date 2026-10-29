#!/usr/bin/env bash
# Run on a fresh Ubuntu 24.04 server after extracting the source to /opt/quant.
set -euo pipefail
if [[ "$(id -u)" != "0" ]]; then
    echo "Run with sudo: sudo bash /opt/quant/deploy/tencent/install-observer.sh" >&2
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
if ! id quant-observer >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/quant-observer --shell /usr/sbin/nologin quant-observer
fi
install -d -o quant-observer -g quant-observer -m 0700 /var/lib/quant-observer
python3.12 -m venv /opt/quant-venv
/opt/quant-venv/bin/python -m pip install --upgrade pip
/opt/quant-venv/bin/python -m pip install --no-cache-dir -e '/opt/quant[crypto]'
install -o root -g root -m 0644 /opt/quant/deploy/tencent/quant-observer.service /etc/systemd/system/quant-observer.service
systemctl daemon-reload
echo "Installed. Test access before starting:"
echo "sudo -u quant-observer timeout 45s /opt/quant-venv/bin/python /opt/quant/scripts/crypto_market_check.py"
echo "If all checks pass: sudo systemctl enable --now quant-observer"
