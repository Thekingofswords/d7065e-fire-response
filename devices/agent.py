"""Decision agent for one room.

The agent reads the room's fire sensors from BuildSim and controls the
room sprinkler through the actuator service. It behaves like a simple
addressable fire panel with sprinkler release:

* smoke or CO   -> ALARM (early warning, no water yet)
* heat          -> SUPPRESSING (sprinkler on); fixed temperature like a
                   68 degC sprinkler bulb, or rate of rise like a
                   rate-of-rise heat detector (8.3 degC/min = 15 degF/min)

Decision flow:

    NORMAL --smoke/CO--> ALARM --heat--> SUPPRESSING
       |                   ^                  |
       +------heat---------|------------------+
                           |                  | fire out (cool and not
                           |                  | rising, after a minimum
                           |                  | discharge time)
                           |                  v
                           +--smoke still--COOLDOWN
                              present          |
                                               v
                                             NORMAL

Each alarm condition has to persist for a few readings before it is acted on
(debounce against sensor noise). Smoke is *not* used to decide that a fire
is out: sprinklers do not remove smoke, so it lingers long after the flames
are gone.

The actuator service remains responsible for validating and applying
commands.
"""

import collections
import json
import os
import time
import urllib.request

import common


def env_float(name, default):
    return float(os.environ.get(name, str(default)))


INTERVAL = env_float("DECISION_INTERVAL", 2.0)

# ---------------------------------------------------------------------------
# Detection thresholds
# ---------------------------------------------------------------------------

# Photoelectric smoke detectors alarm at roughly 2-12 %/m obscuration.
SMOKE_ALARM_THRESHOLD = env_float("SMOKE_ALARM_THRESHOLD", 8.0)    # %/m
CO_ALARM_THRESHOLD = env_float("CO_ALARM_THRESHOLD", 50.0)         # ppm

# Heat: fixed temperature and rate of rise.
HEAT_FIXED_THRESHOLD = env_float("FIRE_TEMP_THRESHOLD", 68.0)      # degC
RATE_OF_RISE_THRESHOLD = env_float("RATE_OF_RISE_THRESHOLD", 8.3)  # degC/min
# Simulated seconds over which the rate of rise is measured.
RATE_OF_RISE_WINDOW_S = env_float("RATE_OF_RISE_WINDOW_SECONDS", 30.0)

# Set to "true" to release the sprinkler on smoke alone (pre-action/deluge
# style) instead of waiting for heat.
SPRINKLER_ON_SMOKE = os.environ.get("SPRINKLER_ON_SMOKE", "false").lower() == "true"

# Consecutive readings required before an alarm condition is acted on.
CONFIRM_TICKS = int(env_float("ALARM_CONFIRM_TICKS", 2))

# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------

# The fire is considered out when the temperature is back below this value
# and no longer rising.
CLEAR_TEMP_THRESHOLD = env_float("CLEAR_TEMP_THRESHOLD", 35.0)
CLEAR_RATE_OF_RISE = env_float("CLEAR_RATE_OF_RISE", 1.0)  # degC/min
# Smoke level below which an alarm resets by itself.
CLEAR_SMOKE_THRESHOLD = env_float("CLEAR_SMOKE_THRESHOLD", 2.0)
CLEAR_CO_THRESHOLD = env_float("CLEAR_CO_THRESHOLD", 15.0)

# Number of consecutive "clear" readings required before the sprinkler
# is switched off.
CLEAR_TICKS_REQUIRED = int(env_float("CLEAR_TICKS_REQUIRED", 3))

# Minimum sprinkler discharge time in *simulated* seconds. A fire that has
# merely been knocked down regrows if the water stops too early.
MIN_DISCHARGE_SECONDS = env_float("MIN_DISCHARGE_SECONDS", 300.0)

# Number of real seconds after sprinkler shutdown before the agent is willing
# to detect another fire.
COOLDOWN_SECONDS = env_float("COOLDOWN_SECONDS", 10.0)

# Inside Docker Compose.
ACTUATOR_URL = os.environ.get(
    "ACTUATOR_URL",
    "http://actuator-a109:8080",
)


# ---------------------------------------------------------------------------
# Sensor functions
# ---------------------------------------------------------------------------

def read_sensor(sensor_id, default=0.0):
    """Read a numeric sensor value from BuildSim."""

    try:
        sensor = common.call(
            "GET",
            f"/api/sensors/{sensor_id}",
        )

        return float(sensor["value"])

    except Exception as exc:  # noqa: BLE001
        print(
            f"failed to read sensor {sensor_id}: {exc}",
            flush=True,
        )

        return default


def read_sprinkler_state():
    """Read the actual sprinkler state from BuildSim."""

    try:
        state = common.call(
            "GET",
            f"/api/actuators/{common.SETPOINT_ID}",
        )["state"]

        return state == "on"

    except Exception as exc:  # noqa: BLE001
        print(
            f"failed to read sprinkler state: {exc}",
            flush=True,
        )

        return False


class RateOfRise:
    """Temperature rate of rise in degC per simulated minute."""

    def __init__(self, window_s):
        self.window_s = window_s
        self.samples = collections.deque()

    def update(self, sim_time, temperature):
        self.samples.append((sim_time, temperature))
        while (
            len(self.samples) > 2
            and sim_time - self.samples[1][0] >= self.window_s
        ):
            self.samples.popleft()
        t0, temp0 = self.samples[0]
        if sim_time - t0 < 0.5 * self.window_s:
            return 0.0
        return (temperature - temp0) / (sim_time - t0) * 60.0


# ---------------------------------------------------------------------------
# Actuator
# ---------------------------------------------------------------------------

def send_actuator_command(actuator, state, reason):
    """Send a command to the actuator service."""

    command = {
        "actuator": actuator,
        "state": state,
        "reason": reason,
    }

    data = json.dumps(command).encode()

    request = urllib.request.Request(
        f"{ACTUATOR_URL}/commands",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=10,
    ) as response:

        raw = response.read()

        return json.loads(raw) if raw else None


def sprinkler(state, reason):
    result = send_actuator_command(actuator="sprinkler", state=state, reason=reason)
    print(f"SPRINKLER {state.upper()}: {result}", flush=True)


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def smoke_alarm(smoke, co):
    return smoke >= SMOKE_ALARM_THRESHOLD or co >= CO_ALARM_THRESHOLD


def heat_alarm(temperature, rate_of_rise):
    return (
        temperature >= HEAT_FIXED_THRESHOLD
        or rate_of_rise >= RATE_OF_RISE_THRESHOLD
    )


def fire_out(temperature, rate_of_rise):
    """Flames are out: cool again and no longer heating up."""
    return (
        temperature <= CLEAR_TEMP_THRESHOLD
        and rate_of_rise <= CLEAR_RATE_OF_RISE
    )


def smoke_cleared(smoke, co):
    return smoke <= CLEAR_SMOKE_THRESHOLD and co <= CLEAR_CO_THRESHOLD


class Debounce:
    """True once a condition has held for `ticks` consecutive readings."""

    def __init__(self, ticks):
        self.ticks = ticks
        self.count = 0

    def update(self, condition):
        self.count = self.count + 1 if condition else 0
        return self.count >= self.ticks

    def reset(self):
        self.count = 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    common.wait_until_healthy()

    state = "NORMAL"
    cooldown_until = 0.0
    discharge_started = 0.0

    ror = RateOfRise(RATE_OF_RISE_WINDOW_S)
    smoke_confirm = Debounce(CONFIRM_TICKS)
    heat_confirm = Debounce(CONFIRM_TICKS)
    out_confirm = Debounce(CLEAR_TICKS_REQUIRED)
    reset_confirm = Debounce(CLEAR_TICKS_REQUIRED)

    start = time.monotonic()

    while True:
        try:
            # -----------------------------------------------------------
            # Read current state
            # -----------------------------------------------------------

            smoke = read_sensor(common.SMOKE_ID)
            temperature = read_sensor(common.TEMP_ID, common.AMBIENT_TEMP_C)
            co = read_sensor(common.CO_ID)
            co2 = read_sensor(common.CO2_ID, common.AMBIENT_CO2_PPM)

            # Don't rely on local memory for the actual actuator state.
            sprinkler_on = read_sprinkler_state()

            now = time.monotonic()
            sim_now = (now - start) * common.TIME_SCALE
            rate_of_rise = ror.update(sim_now, temperature)

            smoke_detected = smoke_confirm.update(smoke_alarm(smoke, co))
            heat_detected = heat_confirm.update(
                heat_alarm(temperature, rate_of_rise)
            )
            release = heat_detected or (SPRINKLER_ON_SMOKE and smoke_detected)

            readings = (
                f"smoke={smoke:.2f}%/m, co={co:.0f}ppm, "
                f"temperature={temperature:.1f}C, "
                f"rate_of_rise={rate_of_rise:.1f}C/min"
            )

            # -----------------------------------------------------------
            # NORMAL / ALARM: watch for a fire
            # -----------------------------------------------------------

            if state in ("NORMAL", "ALARM"):

                if release:
                    if not sprinkler_on:
                        sprinkler(
                            "on",
                            f"Fire confirmed by heat in {common.ROOM}: {readings}",
                        )
                    discharge_started = sim_now
                    out_confirm.reset()
                    state = "SUPPRESSING"

                elif state == "NORMAL" and smoke_detected:
                    print(
                        f"FIRE ALARM in {common.ROOM}: {readings}",
                        flush=True,
                    )
                    reset_confirm.reset()
                    state = "ALARM"

                elif state == "ALARM" and reset_confirm.update(
                    smoke_cleared(smoke, co)
                ):
                    print(f"Alarm reset in {common.ROOM}", flush=True)
                    state = "NORMAL"

            # -----------------------------------------------------------
            # SUPPRESSING
            # -----------------------------------------------------------

            elif state == "SUPPRESSING":

                # Keep the water on if something switched it off.
                if not sprinkler_on:
                    sprinkler(
                        "on",
                        f"Re-opening sprinkler in {common.ROOM}: {readings}",
                    )

                discharged = sim_now - discharge_started
                out = out_confirm.update(fire_out(temperature, rate_of_rise))

                if out:
                    print(
                        f"fire out reading confirmed "
                        f"(discharged {discharged:.0f}s / "
                        f"{MIN_DISCHARGE_SECONDS:.0f}s)",
                        flush=True,
                    )

                if out and discharged >= MIN_DISCHARGE_SECONDS:
                    sprinkler(
                        "off",
                        f"Fire out in {common.ROOM}: {readings}",
                    )
                    cooldown_until = now + COOLDOWN_SECONDS
                    state = "COOLDOWN"

            # -----------------------------------------------------------
            # COOLDOWN
            # -----------------------------------------------------------

            elif state == "COOLDOWN":

                # Ensure the sprinkler is actually off.
                if sprinkler_on:
                    sprinkler(
                        "off",
                        f"Maintaining extinguished state in {common.ROOM}",
                    )

                if now >= cooldown_until:
                    heat_confirm.reset()
                    if smoke_cleared(smoke, co):
                        state = "NORMAL"
                    else:
                        # Smoke lingers after the fire; keep the alarm up
                        # until it has cleared.
                        reset_confirm.reset()
                        state = "ALARM"

                    print(
                        f"Fire response complete in {common.ROOM}; "
                        f"returning to {state}",
                        flush=True,
                    )

            # -----------------------------------------------------------
            # Logging
            # -----------------------------------------------------------

            print(
                f"room={common.ROOM} "
                f"state={state} "
                f"sprinkler={sprinkler_on} "
                f"smoke={smoke:.2f}%/m "
                f"co={co:.0f}ppm "
                f"temperature={temperature:.1f}C "
                f"ror={rate_of_rise:.1f}C/min "
                f"co2={co2:.0f}",
                flush=True,
            )

        except Exception as exc:  # noqa: BLE001
            print(
                f"decision tick failed: {exc}",
                flush=True,
            )

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
