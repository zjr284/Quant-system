#!/usr/bin/env bash
set -euo pipefail
cd /opt/quant
/usr/bin/timeout --signal=TERM --kill-after=5s 45s \
    /opt/quant-venv/bin/python scripts/crypto_market_check.py
exec /opt/quant-venv/bin/quant crypto watch \
    --db /var/lib/quant-observer/observation.sqlite
