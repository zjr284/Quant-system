#!/usr/bin/env python3
"""Send a minimal generic webhook alert for a failed systemd unit."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

url = os.environ.get("QUANT_ALERT_WEBHOOK_URL", "")
if not url:
    sys.exit(0)
payload = json.dumps({"event": "systemd_unit_failed", "unit": sys.argv[1] if len(sys.argv) > 1 else "unknown",
                      "timestamp": datetime.now(timezone.utc).isoformat()}).encode()
request = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=5):
    pass
