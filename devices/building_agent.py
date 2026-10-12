"""Building-wide fire decision agent.

One agent for the whole building: every room and corridor with fire
sensors (topology.py), on all floors. It subscribes to every detector's
readings over MQTT, runs a detector over all zones together, commands each
zone's sprinkler and smoke-purge damper through the actuator gateway and
publishes the current alerts and an event stream.

    sense    building/+/+/sensors (retained, one message per zone per tick)
             building/gateways/+/status (last will: a crashed gateway)
    features features.FeatureExtractor  (history, rates, neighbour context)
    detect   DETECTOR=rules  fixed limits + neighbour attribution
             DETECTOR=model  trained LightGBM model (train_model.py)
             DETECTOR=hybrid the rules plus anomaly detection (isolation
                             forest + autoencoder on normal data) and a cause
                             classifier: fire / nuisance / sensor fault
                             (anomaly.py, train_anomaly.py)
    decide   per-zone state machine with debouncing and a safety net
    act      building/<level>/<room>/actuator/sprinkler/set and
             .../actuator/damper/set (re-sent until the confirmed .../state
             topic matches)
    report   building/alerts (retained), building/events

Per-room states:

    NORMAL --alarm--> ALARM --fire--> SUPPRESSING --out--> COOLDOWN --> NORMAL
       |                                   ^                  |    or ALARM
       +-----------------fire--------------+                  | (smoke left)

Smoke purge: a zone's damper opens (1.0) when its smoke reaches
PURGE_ON_SMOKE and closes (0.0) once it has stayed below PURGE_OFF_SMOKE,
after at least PURGE_MIN_SECONDS. With a faulty smoke sensor it follows the
zone's alarm state instead.

Fire doors: on an alarm, the hold-open devices of the doors on that floor
are released (doors close but stay unlocked, so they still work as escape
routes) and, on a confirmed fire, the lifts are recalled (lift lobbies
blocked). DOOR_RELEASE=level (default) | zone (only the alarmed zones) |
off. Doors return to normal DOOR_RESET_SECONDS after the last alarm.

Fire spread (spread_model.py): every tick the agent predicts, per zone, the
probability of becoming untenable within the next 5 simulated minutes from
the room adjacency graph, temperature gradients and door states, and
publishes the zones at risk on building/risk.

Evacuation: the agent owns the evacuation plan (building/evacuation/plan,
retained): phase (normal / evacuate / all_clear), the floors to evacuate
(the alarm floors and the floors above first, the whole building on a
confirmed fire or after FULL_EVACUATION_SECONDS), fire, smoke and at-risk
zones, door states and usable exits. The occupants service follows it and
reports progress (building/evacuation/progress); people awaiting rescue keep
a critical alert.

Lifecycle (lifecycle.py): Starting -> Syncing -> Running <-> Degraded.
Degraded when a sensor gateway is offline or degraded, or more than
DEGRADED_STALE_FRACTION of the zones are stale. While degraded the agent
never switches water off (it cannot confirm a fire is out) and says so in
an alert.

Safety net: whatever the detector says, a room whose temperature reaches
SAFETY_TEMP_C gets water. A learned model is never the only thing between a
fire and the sprinkler.

The decision logic (BuildingPolicy) contains no network code, so
generate_dataset.py runs exactly the same policy offline.
"""
import json
import math
import os
import time
import uuid

import common
import features as feature_lib
import health
import lifecycle
import messages
import mqtt_bus
import simclock
import spread_model
import topology


def env_float(name, default):
    return float(os.environ.get(name, str(default)))


INTERVAL = env_float("DECISION_INTERVAL", 2.0)
DETECTOR = os.environ.get("DETECTOR", "rules").strip().lower()
MODEL_PATH = os.environ.get("MODEL_PATH", "/models/fire_lgbm.txt")
ANOMALY_MODEL_PATH = os.environ.get("ANOMALY_MODEL_PATH", "/models/anomaly.pkl")
# Hybrid: an anomaly whose likeliest cause is fire with at least this
# probability raises the alarm (water only above the trained release level).
HYBRID_FIRE_ALARM = env_float("HYBRID_FIRE_ALARM", 0.5)
# Spread risk is only reported within this many links (doors, openings,
# stairs) of a zone in alarm.
SPREAD_MAX_HOPS = int(env_float("SPREAD_MAX_HOPS", 3))

# Rules detector.
SMOKE_ALARM_THRESHOLD = env_float("SMOKE_ALARM_THRESHOLD", 8.0)     # %/m
CO_ALARM_THRESHOLD = env_float("CO_ALARM_THRESHOLD", 50.0)          # ppm
HEAT_FIXED_THRESHOLD = env_float("FIRE_TEMP_THRESHOLD", 68.0)       # degC
RATE_OF_RISE_THRESHOLD = env_float("RATE_OF_RISE_THRESHOLD", 8.3)   # degC/min
# A rate of rise releases water only with evidence of combustion: CO, a
# fast rise, or a hot room. Cooking or a heater in a room whose doors the
# fire-door policy has closed warms it by 9-12 degC/min to under 40 degC
# with at most ~5 ppm CO; flaming fires rise faster than 14 degC/min and
# show 10+ ppm CO unless the CO sensor is dead (baseline.py, P6).
RATE_MIN_CO = env_float("RATE_MIN_CO", 10.0)                        # ppm
RATE_FAST = env_float("RATE_FAST", 2 * RATE_OF_RISE_THRESHOLD)      # degC/min
RATE_HOT_C = env_float("RATE_HOT_C", 50.0)
# A rate-of-rise trigger is blamed on a neighbour when that neighbour is
# hotter by at least this much and also heat-alarmed (smoke spilling in).
ATTRIBUTION_MARGIN_C = env_float("ATTRIBUTION_MARGIN_C", 2.0)
CLEAR_SMOKE_THRESHOLD = env_float("CLEAR_SMOKE_THRESHOLD", 2.0)
# Release without a working temperature sensor: dense smoke and CO together.
FALLBACK_SMOKE_THRESHOLD = env_float("FALLBACK_SMOKE_THRESHOLD", 10.0)   # %/m
FALLBACK_CO_THRESHOLD = env_float("FALLBACK_CO_THRESHOLD", 30.0)         # ppm
# Release in a room with no working fire sensors when a neighbour has a
# confirmed fire.
BLIND_ROOM_RELEASE = os.environ.get("BLIND_ROOM_RELEASE", "true").lower() == "true"
# A blind zone (no working fire sensors) also gets water when a neighbour
# sees this much smoke or CO: with the fire doors closed, a fire in a blind
# room shows itself next door only as smoke leaking through the door gaps.
BLIND_NEIGHBOUR_SMOKE = env_float("BLIND_NEIGHBOUR_SMOKE", 2.0)   # %/m
BLIND_NEIGHBOUR_CO = env_float("BLIND_NEIGHBOUR_CO", 15.0)        # ppm
CLEAR_CO_THRESHOLD = env_float("CLEAR_CO_THRESHOLD", 15.0)

# Policy.
CONFIRM_TICKS = int(env_float("ALARM_CONFIRM_TICKS", 2))
CLEAR_TICKS = int(env_float("CLEAR_TICKS_REQUIRED", 3))
SAFETY_TEMP_C = env_float("SAFETY_TEMP_C", 90.0)
CLEAR_TEMP_THRESHOLD = env_float("CLEAR_TEMP_THRESHOLD", 35.0)
CLEAR_RATE_OF_RISE = env_float("CLEAR_RATE_OF_RISE", 1.0)            # degC/min
MIN_DISCHARGE_SECONDS = env_float("MIN_DISCHARGE_SECONDS", 300.0)    # simulated
COOLDOWN_SECONDS = env_float("COOLDOWN_SIM_SECONDS", 50.0)           # simulated
# Longer minimum discharge when the room's temperature sensor is faulty.
FAULT_DISCHARGE_FACTOR = env_float("FAULT_DISCHARGE_FACTOR", 2.0)
# Smoke purge (dampers).
SMOKE_PURGE = os.environ.get("SMOKE_PURGE", "true").lower() == "true"
PURGE_ON_SMOKE = env_float("PURGE_ON_SMOKE", 3.0)                    # %/m
PURGE_OFF_SMOKE = env_float("PURGE_OFF_SMOKE", 0.5)                  # %/m
PURGE_MIN_SECONDS = env_float("PURGE_MIN_SECONDS", 120.0)            # simulated
# Fire doors and lifts.
DOOR_RELEASE = os.environ.get("DOOR_RELEASE", "level").strip().lower()
DOOR_RESET_SECONDS = env_float("DOOR_RESET_SECONDS", 120.0)                # simulated
LIFT_RECALL = os.environ.get("LIFT_RECALL", "true").lower() == "true"
# On any fire alarm, release the access control of every office and
# stairwell door in the building (fail-safe unlock for egress).
EGRESS_UNLOCK = os.environ.get("EGRESS_UNLOCK", "true").lower() == "true"
# Evacuation plan.
FULL_EVACUATION_SECONDS = env_float("FULL_EVACUATION_SECONDS", 300.0)      # simulated
ALL_CLEAR_SECONDS = env_float("ALL_CLEAR_SECONDS", 180.0)                  # simulated
PLAN_SMOKE_THRESHOLD = env_float("PLAN_SMOKE_THRESHOLD", 10.0)             # %/m
EXIT_ROOMS = [r.strip() for r in os.environ.get(
    "EXIT_ROOMS", "1575,A183,A119,A1405,A1015,A104").split(",") if r.strip()]
EXIT_LEVEL = os.environ.get("EXIT_LEVEL", "level0")
# Degraded mode.
DEGRADED_STALE_FRACTION = env_float("DEGRADED_STALE_FRACTION", 0.2)
# A sensor whose BuildSim value is older than this (real seconds) is offline.
STALE_SECONDS = env_float("SENSOR_STALE_SECONDS", 20.0)


def _is_nan(value):
    return isinstance(value, float) and math.isnan(value)


def _value(features, name, default=0.0):
    value = features.get(name, default)
    return default if _is_nan(value) else value


# ---------------------------------------------------------------------------
# Detectors: features of all rooms -> per-room assessment
# ---------------------------------------------------------------------------

class RulesDetector:
    """Fixed limits, with neighbour attribution for heat triggers and
    fallbacks for faulty sensors."""

    name = "rules"

    def assess(self, room_features, neighbours, faults=None):
        faults = faults or {}

        def ok(room, signal):
            return signal not in faults.get(room, {})

        heat = {}
        for room, f in room_features.items():
            if ok(room, "temperature"):
                temperature = _value(f, "temperature", common.AMBIENT_TEMP_C)
                rate = _value(f, "temperature_rate_30")
                combustion = (rate >= RATE_FAST or temperature >= RATE_HOT_C
                              or (ok(room, "co") and _value(f, "co") >= RATE_MIN_CO))
                heat[room] = (
                    temperature >= HEAT_FIXED_THRESHOLD,
                    rate >= RATE_OF_RISE_THRESHOLD and combustion,
                )
            else:
                heat[room] = (False, False)

        result = {}
        for room, f in room_features.items():
            temperature = _value(f, "temperature", common.AMBIENT_TEMP_C)
            smoke = _value(f, "smoke")
            co = _value(f, "co")
            fixed, rate = heat[room]
            fire = fixed or rate
            reason = ""
            if fire:
                reason = "fixed temperature" if fixed else "rate of rise"
            if rate and not fixed:
                for other in neighbours.get(room, []):
                    if other not in room_features or not ok(other, "temperature"):
                        continue
                    other_temp = _value(
                        room_features[other], "temperature", common.AMBIENT_TEMP_C
                    )
                    if any(heat[other]) and other_temp >= temperature + ATTRIBUTION_MARGIN_C:
                        fire = False
                        reason = f"heat attributed to {other}"
                        break

            # Without a working thermometer, heat cannot confirm a fire:
            # fall back to dense smoke together with CO.
            if not ok(room, "temperature") and ok(room, "smoke") and ok(room, "co") and (
                smoke >= FALLBACK_SMOKE_THRESHOLD and co >= FALLBACK_CO_THRESHOLD
            ):
                fire = True
                reason = "smoke + CO (temperature sensor faulty)"

            smoke_alarm = ok(room, "smoke") and smoke >= SMOKE_ALARM_THRESHOLD
            co_alarm = ok(room, "co") and co >= CO_ALARM_THRESHOLD
            result[room] = {
                "p_fire": 1.0 if fire else 0.0,
                "alarm": smoke_alarm or co_alarm or fire,
                "alarm_clear": (not ok(room, "smoke") or smoke <= CLEAR_SMOKE_THRESHOLD)
                and (not ok(room, "co") or co <= CLEAR_CO_THRESHOLD),
                "reason": reason,
            }

        # A room whose fire sensors are all faulty cannot see a fire. Alarm it
        # when a neighbouring room alarms, and give it water as a precaution
        # when a neighbour has a confirmed fire: hot smoke from a fire in the
        # blind room is exactly what the neighbour would be detecting.
        for room in result:
            if all(not ok(room, s) for s in ("temperature", "smoke", "co")):
                alarmed = [n for n in neighbours.get(room, []) if n in result and result[n]["alarm"]]
                burning = [n for n in alarmed if result[n]["p_fire"] >= 0.5]
                fire = BLIND_ROOM_RELEASE and bool(burning)
                result[room].update({
                    "p_fire": 1.0 if fire else 0.0,
                    "alarm": bool(alarmed),
                    "alarm_clear": not alarmed,
                    "reason": (
                        f"room unmonitored, fire next door in {', '.join(burning)}: "
                        f"precautionary release" if fire else
                        f"room unmonitored, alarm in {', '.join(alarmed)}" if alarmed
                        else "room unmonitored"
                    ),
                })
        return result


class ModelDetector:
    """Trained LightGBM model (see train_model.py)."""

    name = "model"

    def __init__(self, path):
        import lightgbm  # only needed in model mode

        self.booster = lightgbm.Booster(model_file=path)
        meta_path = os.path.splitext(path)[0] + ".json"
        with open(meta_path, encoding="utf-8") as handle:
            self.meta = json.load(handle)
        if self.meta["features"] != feature_lib.FEATURE_NAMES:
            raise ValueError(
                "model was trained with a different feature set; retrain it"
            )
        self.release = float(os.environ.get(
            "MODEL_RELEASE_THRESHOLD", self.meta["threshold"]))
        self.alarm = float(os.environ.get(
            "MODEL_ALARM_THRESHOLD", self.meta.get("alarm_threshold", self.release / 2)))
        print(
            f"loaded model {path} (release p>={self.release:.2f}, "
            f"alarm p>={self.alarm:.2f})",
            flush=True,
        )

    def assess(self, room_features, neighbours, faults=None):
        # Faulty signals are already blanked in the features, and the model
        # was trained with the <signal>_ok inputs.
        rooms = list(room_features)
        if not rooms:
            return {}
        rows = [feature_lib.vector(room_features[room]) for room in rooms]
        probabilities = self.booster.predict(rows)
        result = {}
        for room, p in zip(rooms, probabilities):
            p = float(p)
            result[room] = {
                "p_fire": p,
                "fire": p >= self.release,
                "alarm": p >= self.alarm,
                "alarm_clear": p < self.alarm / 2,
                "reason": f"model p={p:.2f}",
            }
        return result


class HybridDetector(RulesDetector):
    """The rules, plus anomaly detection as a first stage and a cause
    classifier as the second (anomaly.py). The rules' decisions always stand;
    the models can only add:

    * anomalous, likeliest cause fire (P >= HYBRID_FIRE_ALARM): alarm; water
      when P(fire) reaches the release level chosen in training (within the
      false-release budget), often before the rules' heat limits
    * anomalous, cause nuisance / sensor fault / unknown: a "check zone"
      alert (decision field "check"), no water
    """

    name = "hybrid"

    def __init__(self, path=None):
        import anomaly
        path = path or os.environ.get("ANOMALY_MODEL_PATH") or ANOMALY_MODEL_PATH
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models",
                             "anomaly.pkl")
        if not os.path.exists(path) and os.path.exists(local):
            path = local    # offline experiments from the repository
        self.model = anomaly.AnomalyModel.load(path)
        self.release = float(os.environ.get(
            "HYBRID_FIRE_RELEASE", self.model.cause_fire_release or 2.0))
        print(f"loaded anomaly model (release P(fire) >= {self.release:.2f})", flush=True)

    def assess(self, room_features, neighbours, faults=None):
        import anomaly
        result = super().assess(room_features, neighbours, faults)
        scores = self.model.assess(room_features)
        for room, (score, probs) in scores.items():
            a = result.get(room)
            if a is None:
                continue
            cause, p = anomaly.top_cause(probs)
            p_fire = (probs or {}).get("fire", 0.0)
            a["anomaly"] = round(score, 2)
            a["cause"] = cause if score >= 1.0 else None
            if score < 1.0:
                continue
            if cause == "fire" and p >= HYBRID_FIRE_ALARM:
                if not a["alarm"]:
                    a["alarm"] = True
                    a["reason"] = a["reason"] or f"anomaly, likely fire (P={p_fire:.2f})"
                if p_fire >= self.release and a["p_fire"] < 0.5:
                    a["p_fire"] = p_fire
                    a["reason"] = f"anomaly classified as fire (P={p_fire:.2f})"
                a["alarm_clear"] = False
            elif a["p_fire"] < 0.5:
                a["check"] = {"normal": "unusual readings", "nuisance": "likely a nuisance "
                              "source (cooking, steam, dust, smoking)", "fault": "likely a sensor "
                              "fault"}.get(cause, "unusual readings")
        return result


def make_detector(kind=None, model_path=None):
    kind = (kind or DETECTOR).lower()
    if kind == "hybrid":
        try:
            return HybridDetector()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: cannot load the anomaly model ({exc}); falling back to the "
                  f"rules detector", flush=True)
    if kind == "model":
        try:
            return ModelDetector(model_path or MODEL_PATH)
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARNING: cannot load model ({exc}); falling back to the "
                f"rules detector",
                flush=True,
            )
    return RulesDetector()


# ---------------------------------------------------------------------------
# Policy: assessments -> per-room states and sprinkler decisions
# ---------------------------------------------------------------------------

class Debounce:
    """True once a condition has held for `ticks` consecutive updates."""

    def __init__(self, ticks):
        self.ticks = ticks
        self.count = 0

    def update(self, condition):
        self.count = self.count + 1 if condition else 0
        return self.count >= self.ticks

    def reset(self):
        self.count = 0


class RoomState:
    def __init__(self):
        self.state = "NORMAL"
        self.fire = Debounce(CONFIRM_TICKS)
        self.alarm = Debounce(CONFIRM_TICKS)
        self.safety = Debounce(CONFIRM_TICKS)
        self.out = Debounce(CLEAR_TICKS)
        self.reset = Debounce(CLEAR_TICKS)
        self.discharge_started = 0.0
        self.cooldown_until = 0.0
        self.reason = ""
        self.purging = False
        self.purge_started = 0.0
        self.purge_clear = Debounce(CLEAR_TICKS)


def _level_order(level):
    """level2 < level10: natural order of level ids."""
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", level)]


class BuildingPolicy:
    def __init__(self, rooms, neighbours, detector=None, corridors=()):
        self.rooms = list(rooms)
        self.neighbours = neighbours
        self.detector = detector or RulesDetector()
        self.extractor = feature_lib.FeatureExtractor(self.rooms, neighbours, corridors)
        self.health = health.SensorHealth(self.rooms)
        self.faults = {}
        self.states = {room: RoomState() for room in self.rooms}
        self.last_assessment = {}
        self.last_features = {}
        # Degraded: inputs cannot be trusted, so water is never switched off.
        self.degraded = False
        self.level_alarm_at = {}   # level -> last sim time with an alarm on it
        self.fire_at = None        # last sim time with a confirmed fire anywhere

    def step(self, sim_time, readings, stale=None):
        """readings: {room: {temperature, smoke, co, co2}} at sim_time;
        stale: {room: set(signals)} whose sensors stopped reporting.

        Returns {room: {"state", "sprinkler", "damper", "p_fire", "reason",
        "faults"}}.
        """
        faults = self.health.update(sim_time, readings, stale)
        self.faults = faults
        room_features = self.extractor.update(sim_time, readings, faults)
        assessment = self.detector.assess(room_features, self.neighbours, faults)
        self._blind_zones(assessment, room_features, faults)
        self.last_features = room_features
        self.last_assessment = assessment

        decisions = {}
        for room, a in assessment.items():
            s = self.states[room]
            f = room_features[room]
            temperature = _value(f, "temperature", common.AMBIENT_TEMP_C)
            rate = _value(f, "temperature_rate_60")
            room_faults = faults.get(room, {})
            temp_ok = "temperature" not in room_faults

            fire = s.fire.update(a.get("fire", a["p_fire"] >= 0.5))
            safety = s.safety.update(temp_ok and temperature >= SAFETY_TEMP_C)
            alarm = s.alarm.update(a["alarm"])

            if s.state in ("NORMAL", "ALARM"):
                if fire or safety:
                    s.state = "SUPPRESSING"
                    s.discharge_started = sim_time
                    s.out.reset()
                    s.reason = "safety net: high temperature" if (safety and not fire) else a["reason"]
                elif s.state == "NORMAL" and alarm:
                    s.state = "ALARM"
                    s.reset.reset()
                    s.reason = a["reason"] or "smoke/CO"
                elif s.state == "ALARM" and s.reset.update(a["alarm_clear"]):
                    s.state = "NORMAL"
                    s.reason = ""

            elif s.state == "SUPPRESSING":
                still_fire = a.get("fire", a["p_fire"] >= 0.5)
                minimum = MIN_DISCHARGE_SECONDS
                if temp_ok:
                    cleared = (temperature <= CLEAR_TEMP_THRESHOLD
                               and rate <= CLEAR_RATE_OF_RISE and not still_fire)
                elif "co" not in room_faults:
                    # A faulty thermometer must never declare the fire out
                    # (a frozen reading looks cool and steady). Use falling
                    # CO and smoke instead, and keep the water on longer.
                    cleared = (
                        _value(f, "co") <= CLEAR_CO_THRESHOLD
                        and _value(f, "co_rate_60") <= 0.5
                        and ("smoke" in room_faults or _value(f, "smoke_rate_60") <= 0.0)
                        and not still_fire
                    )
                    minimum *= FAULT_DISCHARGE_FACTOR
                else:
                    cleared = False  # cannot verify: needs a manual reset
                    s.reason = "fire out cannot be verified (sensors faulty)"
                out = s.out.update(cleared)
                if self.degraded:
                    out = False   # cannot confirm the fire is out on stale data
                if out and sim_time - s.discharge_started >= minimum:
                    s.state = "COOLDOWN"
                    s.cooldown_until = sim_time + COOLDOWN_SECONDS
                    s.reason = "fire out"

            elif s.state == "COOLDOWN":
                if sim_time >= s.cooldown_until:
                    s.fire.reset()
                    s.safety.reset()
                    s.reset.reset()
                    s.state = "NORMAL" if a["alarm_clear"] else "ALARM"
                    s.reason = "" if s.state == "NORMAL" else "smoke remaining"

            if SMOKE_PURGE:
                self._purge(s, sim_time, f, room_faults)

            decisions[room] = {
                "state": s.state,
                "sprinkler": s.state == "SUPPRESSING",
                "damper": 1.0 if s.purging else 0.0,
                "p_fire": a["p_fire"],
                "reason": s.reason,
                "faults": room_faults,
            }
            if "anomaly" in a:
                decisions[room]["anomaly"] = a["anomaly"]
                decisions[room]["cause"] = a.get("cause")
            if a.get("check") and s.state == "NORMAL":
                decisions[room]["check"] = a["check"]
        return decisions

    def doors(self, sim_time, decisions):
        """Fire-door and lift decisions from this tick's zone states.

        Returns {"levels": {level: "closed" | "normal"},
                 "zones": {zone: "closed" | "normal"},
                 "lifts": "blocked" | "normal",
                 "egress": "unlocked" | "normal"}."""
        alarmed = {z for z, d in decisions.items()
                   if d["state"] in ("ALARM", "SUPPRESSING", "COOLDOWN")}
        result = {"levels": {}, "zones": {}, "lifts": "normal", "egress": "normal"}
        if alarmed:
            self.any_alarm_at = sim_time
        last_alarm = getattr(self, "any_alarm_at", None)
        if EGRESS_UNLOCK and last_alarm is not None and \
                sim_time - last_alarm < DOOR_RESET_SECONDS:
            result["egress"] = "unlocked"
        if DOOR_RELEASE == "off":
            return result
        for zone in alarmed:
            level = topology.split(zone)[0]
            self.level_alarm_at[level] = sim_time
            self.level_alarm_at[zone] = sim_time
        for key, last in self.level_alarm_at.items():
            closed = "closed" if sim_time - last < DOOR_RESET_SECONDS else "normal"
            if "/" in key:
                result["zones"][key] = closed
            else:
                result["levels"][key] = closed
        if any(d["state"] == "SUPPRESSING" for d in decisions.values()):
            self.fire_at = sim_time
        if LIFT_RECALL and self.fire_at is not None and \
                sim_time - self.fire_at < DOOR_RESET_SECONDS:
            result["lifts"] = "blocked"
        if DOOR_RELEASE == "level":
            result["zones"] = {}
        else:
            result["levels"] = {}
        return result

    def _blind_zones(self, assessment, room_features, faults):
        """Precautionary water for a zone that cannot see (all fire sensors
        faulty) when a neighbour shows smoke or CO, whatever the detector.
        Without it, closed fire doors would hide a fire in a blind room
        until it flashes over (baseline.py --faults)."""
        if not BLIND_ROOM_RELEASE:
            return
        for room, a in assessment.items():
            room_faults = faults.get(room, {})
            if not all(sig in room_faults for sig in ("temperature", "smoke", "co")):
                continue
            evidence = []
            for other in self.neighbours.get(room, []):
                f = room_features.get(other)
                if f is None:
                    continue
                other_faults = faults.get(other, {})
                if "smoke" not in other_faults and _value(f, "smoke") >= BLIND_NEIGHBOUR_SMOKE:
                    evidence.append(f"smoke {_value(f, 'smoke'):.1f} %/m in {other}")
                elif "co" not in other_faults and _value(f, "co") >= BLIND_NEIGHBOUR_CO:
                    evidence.append(f"CO {_value(f, 'co'):.0f} ppm in {other}")
            if evidence and not a.get("fire", a["p_fire"] >= 0.5):
                a.update(fire=True, alarm=True, alarm_clear=False, p_fire=max(a["p_fire"], 1.0),
                         reason="room unmonitored, " + ", ".join(evidence[:2]) +
                                ": precautionary release")

    def evacuation_plan(self, sim_time, decisions, readings, risk=None, doors=None):
        """The evacuation plan for this tick (without version and time).

        Phases: "normal" -> "evacuate" on any alarm -> "all_clear" once no
        zone has been in alarm for ALL_CLEAR_SECONDS -> "normal" with the
        next tick. Floors: the alarm floors and the floors above them first
        (phased evacuation), every floor on a confirmed fire or after
        FULL_EVACUATION_SECONDS."""
        risk = risk or {}
        doors = doors or {}
        fire = sorted(z for z, d in decisions.items() if d["state"] == "SUPPRESSING")
        alarm = sorted(z for z, d in decisions.items() if d["state"] in ("ALARM", "COOLDOWN"))
        smoke = sorted(z for z, r in readings.items()
                       if r.get("smoke", 0.0) >= PLAN_SMOKE_THRESHOLD and z not in fire)
        levels_all = sorted({topology.split(z)[0] for z in decisions}, key=_level_order)
        prev = getattr(self, "plan_phase", "normal")
        if fire or alarm:
            if prev != "evacuate":
                self.evacuation_started = sim_time
            phase = "evacuate"
            self.last_alarm_plan = sim_time
        elif prev == "evacuate" and sim_time - getattr(self, "last_alarm_plan", sim_time) \
                < ALL_CLEAR_SECONDS:
            phase = "evacuate"
        elif prev == "evacuate":
            phase = "all_clear"
        else:
            phase = "normal"
        self.plan_phase = phase
        levels = []
        if phase == "evacuate":
            source_levels = {topology.split(z)[0] for z in fire + alarm} or set(
                getattr(self, "plan_levels", ()))
            lowest = min((levels_all.index(lv) for lv in source_levels if lv in levels_all),
                         default=0)
            levels = [lv for lv in levels_all if levels_all.index(lv) >= lowest]
            started = getattr(self, "evacuation_started", sim_time)
            if fire or sim_time - started >= FULL_EVACUATION_SECONDS:
                levels = list(levels_all)
            # Never shrink the floors being evacuated during one evacuation.
            levels = sorted(set(levels) | set(getattr(self, "plan_levels", ()) or ()),
                            key=levels_all.index)
        self.plan_levels = levels
        blocked_exits = {z for z in fire + smoke if topology.split(z)[0] == EXIT_LEVEL}
        exits = [e for e in EXIT_ROOMS if topology.key(EXIT_LEVEL, e) not in blocked_exits]
        return {
            "phase": phase,
            "evacuate_levels": levels,
            "fire_zones": fire,
            "alarm_zones": alarm,
            "smoke_zones": smoke,
            # Zone names only: the probabilities are on building/risk, and the
            # plan (versioned, retained) changes only when the situation does.
            "risk_zones": sorted(z for z in risk if z not in fire),
            "exits": exits,
            "doors": {"egress": doors.get("egress", "normal"),
                      "closed_levels": sorted(lv for lv, st in doors.get("levels", {}).items()
                                              if st == "closed"),
                      "lifts": doors.get("lifts", "normal")},
        }

    @staticmethod
    def _purge(s, sim_time, f, room_faults):
        """Open the smoke-purge damper on smoke, close it once clear."""
        smoke_ok = "smoke" not in room_faults
        smoke = _value(f, "smoke")
        alarmed = s.state in ("ALARM", "SUPPRESSING", "COOLDOWN")
        if not s.purging:
            if (smoke_ok and smoke >= PURGE_ON_SMOKE) or (not smoke_ok and alarmed):
                s.purging = True
                s.purge_started = sim_time
                s.purge_clear.reset()
            return
        clear = s.purge_clear.update(smoke <= PURGE_OFF_SMOKE if smoke_ok else not alarmed)
        if clear and sim_time - s.purge_started >= PURGE_MIN_SECONDS:
            s.purging = False


# ---------------------------------------------------------------------------
# MQTT I/O
# ---------------------------------------------------------------------------

SIGNALS = ("temperature", "smoke", "co", "co2")
RESEND_SECONDS = env_float("COMMAND_RESEND_SECONDS", 10.0)   # real seconds
DATA_QUALITY_REPORT_SECONDS = env_float("DATA_QUALITY_REPORT_SECONDS", 30.0)
DOOR_RESEND_SECONDS = env_float("DOOR_RESEND_SECONDS", 30.0)               # real seconds


class door_gateway_topics:   # noqa: N801 - a namespace for the door command topics
    LIFTS = "building/lifts/set"
    EGRESS = "building/doors/egress/set"

    @staticmethod
    def level(level):
        return f"building/{mqtt_bus.enc(level)}/doors/set"

    @staticmethod
    def zone(zone):
        level, room = topology.split(zone)
        return f"building/{mqtt_bus.enc(level)}/{mqtt_bus.enc(room)}/doors/set"
ALERT_PRIORITY = {"critical": 0, "warning": 1, "info": 2}


def build_alerts(decisions, readings, offline_gateways=None, degraded=None,
                 gateway_status=None):
    """Current alerts, most severe first. A crashed gateway gives one alert
    rather than one per zone it served (BuildSim shows at most 100)."""
    offline_gateways = offline_gateways or {}
    gateway_status = gateway_status or {}
    dark = {zone for zones in offline_gateways.values() for zone in zones}
    alerts = [{
        "id": f"gateway-{name}", "severity": "critical",
        "title": f"Sensor gateway {name} {gateway_status.get(name, 'offline')}",
        "message": f"{len(zones)} zones unmonitored; rooms next to an alarm are alarmed "
                   f"as a precaution",
    } for name, zones in sorted(offline_gateways.items())]
    if degraded:
        alerts.append({
            "id": "agent-degraded", "severity": "critical",
            "title": "Fire agent degraded",
            "message": f"{degraded}; sprinklers stay on until fresh data confirms the fire "
                       f"is out",
        })
    for room, d in decisions.items():
        level, name = topology.split(room)
        r = readings.get(room, {})
        summary = (
            f"smoke {r.get('smoke', 0):.1f} %/m, CO {r.get('co', 0):.0f} ppm, "
            f"{r.get('temperature', 0):.0f} °C"
        )
        if d["state"] == "SUPPRESSING":
            alerts.append({
                "id": f"fire-{level}-{name}", "severity": "critical",
                "title": f"Fire in {name}: sprinkler released",
                "message": f"{d['reason']}; {summary}",
                "level": level, "room": name,
            })
        elif d["state"] == "ALARM":
            alerts.append({
                "id": f"fire-{level}-{name}", "severity": "warning",
                "title": f"Fire alarm in {name}",
                "message": f"{d['reason']}; {summary}",
                "level": level, "room": name,
            })
        elif d["state"] == "COOLDOWN":
            alerts.append({
                "id": f"fire-{level}-{name}", "severity": "info",
                "title": f"Fire out in {name}",
                "message": summary, "level": level, "room": name,
            })
        if d.get("check"):
            alerts.append({
                "id": f"check-{level}-{name}", "severity": "warning",
                "title": f"Check {name}: unusual readings",
                "message": f"anomaly score {d.get('anomaly', 0):.1f}, {d['check']}; {summary}",
                "level": level, "room": name,
            })
        if d.get("faults") and room not in dark:
            alerts.append({
                "id": f"sensor-{level}-{name}", "severity": "warning",
                "title": f"Sensor fault in {name}",
                "message": f"{health.describe(d['faults'])}; " + (
                    "no working fire sensors: the room is unmonitored and relies "
                    "on its neighbours" if {"temperature", "smoke", "co"} <= d["faults"].keys()
                    else "detection in this room relies on the remaining sensors"),
                "level": level, "room": name,
            })
    alerts.sort(key=lambda a: (ALERT_PRIORITY[a["severity"]], not a["id"].startswith("fire-"), a["id"]))
    return alerts


class AgentBus:
    def __init__(self):
        self.readings = {}   # zone -> latest sensor message
        self.actual = {}     # (zone, actuator) -> confirmed state
        self.corrected = set()  # confirmations the actuator gateway corrected at its start
        self.gateways = {}   # gateway -> "online"/"offline"
        self.validator = messages.SensorValidator()
        self.gateway_detail = {}
        self.life = lifecycle.Lifecycle("building-agent", status_topic=mqtt_bus.AGENT_STATUS_TOPIC)
        self.bus = self.life.bus
        self.bus.subscribe(mqtt_bus.SENSORS_SUB, self._on_sensors, qos=0)
        self.bus.subscribe(mqtt_bus.ACTUATOR_STATE_SUB, self._on_state)
        self.bus.subscribe(mqtt_bus.GATEWAY_STATUS_SUB, self._on_gateway)
        self.door_states = {}   # door id -> "open" | "closed" | "blocked"
        self.progress = {}
        self.clock = simclock.Follower()
        self.bus.subscribe(mqtt_bus.DOOR_STATE_SUB, self._on_door)
        self.bus.subscribe(mqtt_bus.EVAC_PROGRESS_TOPIC, self._on_progress)
        self.bus.subscribe(mqtt_bus.CLOCK_TOPIC, self.clock.on_message, qos=0)

    def _on_door(self, topic, payload):
        if isinstance(payload, dict):
            self.door_states[mqtt_bus.dec(topic.split("/")[2])] = payload.get("state")

    def _on_progress(self, topic, payload):
        if isinstance(payload, dict):
            self.progress = payload

    def _on_sensors(self, topic, payload):
        level, room, _ = mqtt_bus.parse_room_topic(topic)
        if not level:
            return
        zone = f"{level}/{room}"
        # Corrupt, impossible, duplicate or out-of-order messages never
        # reach the policy (messages.py); they are reported as data quality.
        clean = self.validator.check(zone, payload)
        if clean is not None:
            self.readings[zone] = clean

    def _on_state(self, topic, payload):
        level, room, rest = mqtt_bus.parse_room_topic(topic)
        if level and len(rest) >= 2 and isinstance(payload, dict):
            key = (f"{level}/{room}", rest[1])
            self.actual[key] = payload.get("state")
            if payload.get("corrected"):
                self.corrected.add(key)

    def _on_gateway(self, topic, payload):
        name = mqtt_bus.dec(topic.split("/")[2])
        status = payload.get("status") if isinstance(payload, dict) else str(payload)
        if isinstance(payload, dict) and payload.get("detail") and status != "online":
            self.gateway_detail[name] = payload["detail"]
        if self.gateways.get(name) != status:
            print(f"gateway {name}: {status}", flush=True)
        self.gateways[name] = status

    def snapshot(self, now):
        """(readings, stale) for the policy. A zone is stale when its last
        message is older than STALE_SECONDS or its gateway is offline."""
        readings, stale = {}, {}
        for zone, msg in list(self.readings.items()):
            try:
                readings[zone] = {s: float(msg[s]) for s in SIGNALS}
            except (KeyError, TypeError, ValueError):
                continue
            old = now - float(msg.get("ts", 0)) > STALE_SECONDS
            if old or self.gateways.get(msg.get("gateway"), "online") != "online":
                stale[zone] = set(SIGNALS)
        return readings, stale

    def command(self, zone, actuator, state, reason):
        level, room = topology.split(zone)
        command_id = uuid.uuid4().hex[:12]
        self.bus.publish(mqtt_bus.actuator_set_topic(level, room, actuator), {
            "state": state, "reason": reason, "id": command_id,
            "ts": time.time(), "source": "building-agent"}, qos=1)
        return command_id

    def door_command(self, topic, state, reason):
        """state: a door state ("closed", "blocked", "normal") or, for the
        egress topic, a lock state ("unlocked", "normal")."""
        command_id = uuid.uuid4().hex[:12]
        field = "lock_state" if topic == door_gateway_topics.EGRESS else "state"
        self.bus.publish(topic, {field: state, "reason": reason, "id": command_id,
                                 "ts": time.time(), "source": "building-agent"}, qos=1)
        return command_id

    def event(self, *args, **kwargs):
        self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(*args, **kwargs), qos=1)


def same_state(actuator, desired, actual):
    if actuator == "damper":
        try:
            return abs(float(desired) - float(actual)) < 1e-6
        except (TypeError, ValueError):
            return False
    return desired == actual


def main():
    common.wait_until_healthy()   # BuildSim serves the floor plans

    topo = topology.Topology.load(common.BASE)
    rooms = topo.sensor_zones()
    neighbours = topo.neighbour_map(rooms)
    corridors = {k for k in rooms if topo.zones[k].type == "corridor"}
    policy = BuildingPolicy(rooms, neighbours, make_detector(), corridors)
    agent = AgentBus()
    life = agent.life
    life.start()

    print(f"building agent for {len(rooms)} zones (detector={policy.detector.name}, "
          f"time scale x{common.TIME_SCALE})", flush=True)

    # Give retained readings and actuator states a moment to arrive, so a
    # restarted agent sees running sprinklers before its first decision.
    time.sleep(3)
    life.running(f"{len(rooms)} zones, detector {policy.detector.name}")

    # Decisions run on the building clock (sim/clock), monotonic and with
    # bounded steps (simclock.PolicyClock).
    policy_clock = simclock.PolicyClock(agent.clock, max_step=10 * INTERVAL * common.TIME_SCALE)
    spread_features = spread_model.SpreadFeatures(topo, rooms)
    spread = spread_model.SpreadModel()
    door_pairs = {did: pair for did, pair, _, _ in topo.doors()}
    last_risk = None
    last_plan = None
    plan_version = 0
    last_alerts = None
    last_faults = {}
    last_states = {}
    pending = {}   # (zone, actuator) -> (desired state, monotonic time sent)
    last_quality = time.monotonic()
    door_sent = {}   # topic -> (state, monotonic time sent)
    tick = 0

    while not life.stopping:
        try:
            life.beat()
            readings, stale = agent.snapshot(time.time())
            sim_time = policy_clock.now()

            # Degraded mode: a gateway is down, or too many zones are stale.
            bad_gateways = sorted(g for g, st in agent.gateways.items()
                                  if st != "online" and g.startswith("sensors"))
            stale_share = len(stale) / max(1, len(rooms))
            reasons = []
            if bad_gateways:
                reasons.append("sensor gateway " + ", ".join(
                    f"{g} {agent.gateways[g]}" for g in bad_gateways))
            if stale_share > DEGRADED_STALE_FRACTION:
                reasons.append(f"{stale_share:.0%} of zones without fresh readings")
            if not readings:
                reasons.append("no sensor readings at all")
            policy.degraded = bool(reasons)
            if reasons:
                life.degraded("; ".join(reasons))
            else:
                life.ok()

            decisions = policy.step(sim_time, readings, stale)

            # Never switch off water the agent did not start: a sprinkler that
            # is confirmed on while its zone is NORMAL/ALARM (agent restart,
            # manual release) is adopted and only stops through the normal
            # "fire out" rules.
            # A sprinkler adopted from a stale retained confirmation (left over
            # from a previous run) that the actuator gateway has since
            # corrected from BuildSim is not running: drop the adoption.
            for key in list(agent.corrected):
                agent.corrected.discard(key)
                room = key[0]
                s_ = policy.states.get(room)
                if key[1] == "sprinkler" and s_ is not None and s_.state == "SUPPRESSING" \
                        and "adopted" in (s_.reason or "") and agent.actual.get(key) == "off":
                    s_.state, s_.reason = "NORMAL", ""
                    s_.fire.reset()
                    decisions[room].update(state="NORMAL", sprinkler=False, reason="")
                    pending.pop(key, None)
                    print(f"{room}: adoption dropped (stale confirmation corrected)", flush=True)

            for room, d in decisions.items():
                if agent.actual.get((room, "sprinkler")) == "on" and d["state"] in ("NORMAL", "ALARM") \
                        and (room, "sprinkler") not in pending:
                    s_ = policy.states[room]
                    s_.state, s_.discharge_started = "SUPPRESSING", sim_time
                    s_.out.reset()
                    s_.reason = "running sprinkler adopted (agent restart or manual release)"
                    d.update(state="SUPPRESSING", sprinkler=True, reason=s_.reason)
                    print(f"{room}: adopting running sprinkler", flush=True)

            for room, d in decisions.items():
                level, name = topology.split(room)
                if last_states.get(room, "NORMAL") != d["state"]:
                    severity = {"SUPPRESSING": "critical", "ALARM": "warning"}.get(d["state"], "info")
                    print(f"{room}: {last_states.get(room, 'NORMAL')} -> {d['state']} ({d['reason']})",
                          flush=True)
                    agent.event("room_state", f"{last_states.get(room, 'NORMAL')} -> {d['state']}: "
                                f"{d['reason']}", level, name, severity=severity,
                                state=d["state"], previous=last_states.get(room, "NORMAL"),
                                p_fire=round(d["p_fire"], 3))
                    last_states[room] = d["state"]

                for actuator, desired, default in (
                        ("sprinkler", "on" if d["sprinkler"] else "off", "off"),
                        ("damper", str(float(d.get("damper", 0.0))), "0.0")):
                    if actuator == "damper" and not SMOKE_PURGE:
                        continue
                    actual = agent.actual.get((room, actuator), default)
                    if same_state(actuator, desired, actual):
                        pending.pop((room, actuator), None)
                        continue
                    sent = pending.get((room, actuator))
                    if sent and sent[0] == desired and time.monotonic() - sent[1] < RESEND_SECONDS:
                        continue   # waiting for the actuator gateway to confirm
                    if actuator == "damper" and not sent:
                        opening = float(desired) > 0
                        print(f"{room}: SMOKE PURGE {'ON' if opening else 'OFF'}", flush=True)
                        agent.event("smoke_purge", "damper opened" if opening else "damper closed",
                                    level, name, severity="info", damper=float(desired))
                    reason = f"{d['state']}: {d['reason']}" if actuator == "sprinkler" else (
                        "smoke purge" if float(desired) > 0 else "smoke cleared")
                    command_id = agent.command(room, actuator, desired, reason)
                    pending[(room, actuator)] = (desired, time.monotonic())
                    if actuator == "sprinkler":
                        print(f"{room}: SPRINKLER {desired.upper()} requested ({command_id})",
                              flush=True)

            if policy.faults != last_faults:
                for room in sorted(set(policy.faults) | set(last_faults)):
                    now_faults = policy.faults.get(room, {})
                    if now_faults != last_faults.get(room, {}):
                        level, name = topology.split(room)
                        text = health.describe(now_faults) or "all sensors OK"
                        print(f"{room}: SENSOR HEALTH {text}", flush=True)
                        agent.event("sensor_health", text, level, name,
                                    severity="warning" if now_faults else "info",
                                    faults=now_faults)
                last_faults = {k: dict(v) for k, v in policy.faults.items()}

            if time.monotonic() - last_quality >= DATA_QUALITY_REPORT_SECONDS:
                last_quality = time.monotonic()
                counts, examples = agent.validator.take_counts()
                if counts:
                    text = ", ".join(f"{n} {reason}" for reason, n in sorted(counts.items()))
                    print(f"DATA QUALITY: rejected {text}; e.g. "
                          f"{'; '.join(f'{z}: {d}' for z, d in examples.values())}", flush=True)
                    agent.event("data_quality", f"rejected sensor messages: {text}",
                                severity="warning", rejected=counts,
                                examples={r: list(e) for r, e in examples.items()})

            # Fire doors and lifts.
            doors = policy.doors(sim_time, decisions)
            wanted = {door_gateway_topics.level(lv): st for lv, st in doors["levels"].items()}
            wanted.update({door_gateway_topics.zone(z): st for z, st in doors["zones"].items()})
            if LIFT_RECALL:
                wanted[door_gateway_topics.LIFTS] = doors["lifts"]
            if EGRESS_UNLOCK:
                wanted[door_gateway_topics.EGRESS] = doors["egress"]
            for topic, state in wanted.items():
                sent = door_sent.get(topic)
                if sent is None and state == "normal":
                    continue   # never commanded: leave the doors alone
                if sent and sent[0] == state and (
                        state == "normal" or time.monotonic() - sent[1] < DOOR_RESEND_SECONDS):
                    continue
                reason = ("fire alarm: hold-open devices released" if state == "closed" else
                          "fire confirmed: lifts recalled" if state == "blocked" else
                          "fire alarm: access control released for egress"
                          if state == "unlocked" else
                          "all clear: doors back to normal")
                agent.door_command(topic, state, reason)
                if not sent or sent[0] != state:
                    print(f"DOORS {topic}: {state} ({reason})", flush=True)
                    agent.event("door_policy", f"{topic}: {state} ({reason})",
                                severity="warning" if state != "normal" else "info",
                                topic=topic, state=state)
                door_sent[topic] = (state, time.monotonic())

            # Fire-spread prediction.
            door_open = {}
            for did, state in list(agent.door_states.items()):
                pair = door_pairs.get(did)
                if pair is not None:
                    door_open[pair] = state == "open"
            rows = spread_features.rows(policy.last_features, decisions, door_open)
            risk = spread.at_risk(spread.predict(rows), decisions)
            # Only near an alarm: the model was trained on neighbourhoods
            # around fires and over-predicts in quiet parts of the building.
            alarmed = {z for z, d in decisions.items()
                       if d["state"] in ("ALARM", "SUPPRESSING", "COOLDOWN")}
            near = spread_features.hops(alarmed) if alarmed else {}
            risk = {z: p for z, p in risk.items() if near.get(z, SPREAD_MAX_HOPS + 1)
                    <= SPREAD_MAX_HOPS}
            risk_zones = {z: round(p, 2) for z, p in risk.items()}
            # Publish on a new set of zones or a probability moving by 0.1+.
            if last_risk is None or set(risk_zones) != set(last_risk or {}) or any(
                    abs(p - (last_risk or {}).get(z, 0.0)) >= 0.1 for z, p in risk_zones.items()):
                agent.bus.publish(mqtt_bus.RISK_TOPIC, {
                    "zones": risk_zones, "horizon_s": spread_model.HORIZON_S,
                    "model": spread.name, "threshold": spread.threshold,
                    "ts": time.time()}, qos=1, retain=True)
                if set(risk_zones) != set(last_risk or {}):
                    added = sorted(set(risk_zones) - set(last_risk or {}))
                    if added:
                        print(f"SPREAD RISK: {', '.join(added[:8])}"
                              f"{' ...' if len(added) > 8 else ''}", flush=True)
                    agent.event("spread_risk", f"{len(risk_zones)} zones at risk of fire spread",
                                severity="warning" if risk_zones else "info",
                                zones=risk_zones, model=spread.name)
                last_risk = risk_zones

            # Evacuation plan.
            plan = policy.evacuation_plan(sim_time, decisions, readings, risk_zones, doors)
            if plan != last_plan:
                plan_version += 1
                if plan["phase"] != (last_plan or {}).get("phase"):
                    print(f"EVACUATION PLAN v{plan_version}: {plan['phase']} "
                          f"{','.join(plan['evacuate_levels'])}", flush=True)
                agent.bus.publish(mqtt_bus.EVAC_PLAN_TOPIC, dict(
                    plan, version=plan_version, clock=agent.clock.label(), ts=time.time()),
                    qos=1, retain=True)
                if plan["phase"] != (last_plan or {}).get("phase") or \
                        plan["evacuate_levels"] != (last_plan or {}).get("evacuate_levels"):
                    agent.event("evacuation_plan",
                                f"v{plan_version}: {plan['phase']} "
                                f"{', '.join(plan['evacuate_levels']) or ''}".strip(),
                                severity="critical" if plan["phase"] == "evacuate" else "info",
                                version=plan_version, phase=plan["phase"],
                                levels=plan["evacuate_levels"])
                last_plan = plan

            offline = {}
            for zone, msg in agent.readings.items():
                gateway = msg.get("gateway")
                if agent.gateways.get(gateway, "online") != "online":
                    offline.setdefault(gateway, []).append(zone)
            alerts = build_alerts(decisions, readings, offline,
                                  degraded=life.detail if policy.degraded else None,
                                  gateway_status=agent.gateways)
            waiting = agent.progress.get("awaiting_rescue") or {}
            if waiting:
                alerts.insert(0, {
                    "id": "people-awaiting-rescue", "severity": "critical",
                    "title": f"{sum(waiting.values())} people awaiting rescue",
                    "message": "; ".join(f"{n} in {z}" for z, n in sorted(waiting.items())),
                })
            if alerts != last_alerts:
                agent.bus.publish(mqtt_bus.ALERTS_TOPIC, alerts, qos=1, retain=True)
                last_alerts = alerts

            tick += 1
            if tick % 15 == 1:
                counts = {}
                for d in decisions.values():
                    counts[d["state"]] = counts.get(d["state"], 0) + 1
                print(f"{len(readings)} zones reporting, {len(stale)} stale, "
                      f"{sum(1 for g in agent.gateways.values() if g == 'online')} gateways online, "
                      f"states {counts}", flush=True)

        except Exception as exc:  # noqa: BLE001
            print(f"decision tick failed: {exc}", flush=True)

        life.sleep(INTERVAL)
    life.stop()


if __name__ == "__main__":
    main()
