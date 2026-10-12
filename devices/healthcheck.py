"""Docker health check: healthy while the service's main loop is alive.

    healthcheck: {test: ["CMD", "python", "healthcheck.py"]}

Fails when HEARTBEAT_FILE (default /tmp/heartbeat) is missing or older than
HEALTH_MAX_AGE_SECONDS (default 30), i.e. when the loop has hung.
"""
import os
import sys
import time

path = os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat")
max_age = float(os.environ.get("HEALTH_MAX_AGE_SECONDS", "30"))
try:
    age = time.time() - os.path.getmtime(path)
except OSError:
    print(f"no heartbeat at {path}")
    sys.exit(1)
if age > max_age:
    print(f"heartbeat {age:.0f}s old (limit {max_age:.0f}s)")
    sys.exit(1)
print(f"healthy (heartbeat {age:.0f}s old)")
