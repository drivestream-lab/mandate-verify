"""Container healthcheck: the poll loop wrote its heartbeat recently."""

import sys
import time
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "/var/lib/mandate-runner/heartbeat")
try:
    age = time.time() - int(path.read_text().strip())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if age < 180 else 1)
