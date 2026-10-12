"""Message schemas and input validation for the MQTT bus.

Every consumer of sensor readings (agent, BuildSim bridge, database writer)
runs incoming messages through `SensorValidator` first. A message is
rejected, never "repaired", when it is:

    malformed      not a JSON object (corrupt bytes, a list, a string)
    missing        a required field is absent
    not_numeric    a field is not a number, or is NaN / infinity
    out_of_range   physically impossible (beyond the instrument's range)
    future         timestamped more than MAX_CLOCK_SKEW_S in the future
    duplicate      same or older timestamp than the last accepted message
                   for that zone (re-delivery or out-of-order)

Rejections are counted per reason, so the agent can report them as
data-quality events. Readings that are valid but *stale* (a sensor that
stopped reporting) or *frozen* (stuck value) are handled later, by
health.py, because they can only be detected over time.

Schemas (field: type, unit, range)

    building/<level>/<room>/sensors
        ts           float  s since epoch       required
        gateway      str                        required
        temperature  float  degC   -40 .. 1200  required
        smoke        float  %/m      0 .. 100   required
        co           float  ppm      0 .. 10000 required
        co2          float  ppm      0 .. 100000 required

    building/<level>/<room>/actuator/<name>/set
        state        "on"/"off" (sprinkler) or "0.0".."1.0" (damper)
        reason, id, ts, source

    building/<level>/<room>/actuator/<name>/state
        state, ts, command_id, reason, source
"""
import collections
import math
import time

MAX_CLOCK_SKEW_S = 60.0

SENSOR_FIELDS = {
    "temperature": (-40.0, 1200.0),
    "smoke": (0.0, 100.0),
    "co": (0.0, 10000.0),
    "co2": (0.0, 100000.0),
}


class Invalid(ValueError):
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _number(payload, field):
    if field not in payload:
        raise Invalid("missing", field)
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid("not_numeric", f"{field}={value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise Invalid("not_numeric", f"{field}={value!r}")
    return value


def parse_sensor(payload, now=None):
    """Validate one sensor message; returns a clean dict or raises Invalid."""
    if not isinstance(payload, dict):
        raise Invalid("malformed", type(payload).__name__)
    ts = _number(payload, "ts")
    now = time.time() if now is None else now
    if ts > now + MAX_CLOCK_SKEW_S:
        raise Invalid("future", f"ts={ts:.0f}")
    clean = {"ts": ts, "gateway": str(payload.get("gateway", ""))}
    for field, (low, high) in SENSOR_FIELDS.items():
        value = _number(payload, field)
        if not low <= value <= high:
            raise Invalid("out_of_range", f"{field}={value}")
        clean[field] = value
    return clean


class SensorValidator:
    """Per-zone validation with duplicate detection and rejection counts."""

    def __init__(self):
        self.last_ts = {}
        self.rejected = collections.Counter()
        self.accepted = 0
        self.examples = {}   # reason -> (zone, detail) of the latest rejection

    def check(self, zone, payload, now=None):
        """Return the clean reading, or None if rejected."""
        try:
            clean = parse_sensor(payload, now)
            last = self.last_ts.get(zone)
            if last is not None and clean["ts"] <= last:
                raise Invalid("duplicate", f"ts={clean['ts']:.3f} <= {last:.3f}")
        except Invalid as exc:
            self.rejected[exc.reason] += 1
            self.examples[exc.reason] = (zone, str(exc))
            return None
        self.last_ts[zone] = clean["ts"]
        self.accepted += 1
        return clean

    def take_counts(self):
        """Rejections since the last call (and reset)."""
        counts, examples = dict(self.rejected), dict(self.examples)
        self.rejected.clear()
        self.examples.clear()
        return counts, examples
