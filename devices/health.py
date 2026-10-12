"""Sensor health checks shared by the building agent, the dataset generator
and the training script.

Two failure modes are detected per room and signal:

* frozen: the reading has not changed at all for FROZEN_SECONDS (simulated).
  A working sensor always jitters a little (noise, last-digit flicker), so
  an exactly constant value means the sensor is stuck. Values at the end of
  the measuring range are exempt: clean air reads 0 %/m smoke and 0 ppm CO,
  and a heat detector in a fire sits at its maximum. Those are legitimate
  flat lines (and, honestly, a sensor stuck at 0 cannot be told apart from
  clean air this way; real detectors use a self-test for that).
* offline: no new value has arrived. The building agent passes these in
  from BuildSim's per-sensor timestamps (a crashed sensor process leaves
  its last value in BuildSim, which would otherwise look plausible).

A signal recovers as soon as it changes again / reports again.
"""
import os

SIGNALS = ("temperature", "smoke", "co", "co2")
SIGNAL_BITS = {signal: 1 << i for i, signal in enumerate(SIGNALS)}

FROZEN_SECONDS = float(os.environ.get("SENSOR_FROZEN_SECONDS", "180"))

# Measuring ranges (must match sensor.py). A reading within EDGE of a range
# limit is not judged.
RANGES = {
    "temperature": (-20.0, float(os.environ.get("TEMP_SENSOR_MAX_C", "150"))),
    "smoke": (0.0, float(os.environ.get("SMOKE_SENSOR_MAX_PCT_M", "30"))),
    "co": (0.0, float(os.environ.get("CO_SENSOR_MAX_PPM", "1000"))),
    "co2": (0.0, float(os.environ.get("CO2_SENSOR_MAX_PPM", "10000"))),
}
EDGE = {"temperature": 0.05, "smoke": 0.005, "co": 0.5, "co2": 0.5}


def mask_from_signals(signals):
    mask = 0
    for signal in signals or ():
        mask |= SIGNAL_BITS[signal]
    return mask


def signals_from_mask(mask):
    return {s for s, bit in SIGNAL_BITS.items() if int(mask or 0) & bit}


def describe(faults):
    """{"temperature": "frozen"} -> "temperature frozen"."""
    return ", ".join(f"{signal} {reason}" for signal, reason in sorted(faults.items()))


class SensorHealth:
    def __init__(self, rooms):
        self.rooms = list(rooms)
        self.last = {}    # (room, signal) -> last value
        self.since = {}   # (room, signal) -> time the value last changed

    def update(self, sim_time, readings, stale=None):
        """Return {room: {signal: "frozen" | "offline"}} for faulty signals.

        readings: {room: {signal: value}}; stale: {room: set(signals)} of
        signals whose sensor stopped reporting.
        """
        stale = stale or {}
        faults = {}
        for room in self.rooms:
            room_faults = {}
            reading = readings.get(room)
            for signal in stale.get(room, ()):
                room_faults[signal] = "offline"
            if reading is not None:
                for signal in SIGNALS:
                    if signal in room_faults or signal not in reading:
                        continue
                    value = reading[signal]
                    key = (room, signal)
                    low, high = RANGES[signal]
                    at_limit = value <= low + EDGE[signal] or value >= high - EDGE[signal]
                    if at_limit or self.last.get(key) != value:
                        self.since[key] = sim_time
                    self.last[key] = value
                    if sim_time - self.since.get(key, sim_time) >= FROZEN_SECONDS:
                        room_faults[signal] = "frozen"
            if room_faults:
                faults[room] = room_faults
        return faults
