"""Multi-room compartment fire simulator for BuildSim.

The simulator owns the *physics*. Every tick it advances a simplified zone
model of each room and publishes the result to BuildSim as

* room layers ("simulation truth"): heat release rate, ceiling gas
  temperature, ceiling-jet velocity, smoke obscuration, visibility, CO, CO2,
  O2 and smoke-layer height. Sensors read these and add their own
  instrument behaviour (response lag, noise, range).
* visual effects: fire, smoke and sprinkler primitives for the 3D viewer.

It reads every zone's sprinkler and smoke-purge damper state back from
BuildSim (GET /api/equipment?category=fire_suppression, one request per
tick), so the closed loop runs through BuildSim:

    fire -> room layers (BuildSim) -> sensor gateway -> MQTT -> agent
         -> actuator gateway -> actuator state (BuildSim) -> fire

Ground-truth labels (fire phase, origin, nuisance source, scenario id) and
the true physical values are also published on sim/truth/<level>/<room> for
the database (training and evaluation only; no device reads them).

Physics, per room (all standard fire-engineering correlations):

* Incipient phase: an optional smouldering period with low heat output but
  smoky, CO-rich combustion, before flaming ignition.
* Growth: t-squared fire, Q = alpha * t^2 (NFPA 72 / SFPE growth classes).
* Heat release is capped by the fuel (heat release per floor area) and by
  the air the doorway lets in, Q_vent = 1500 * A_o * sqrt(H_o) kW (Kawagoe).
* Hot-layer temperature: McCaffrey-Quintiere-Harkleroad (MQH), approached
  with a thermal lag; at the detector the Alpert ceiling-jet excess is added.
* Flashover when the hot layer reaches ~600 degC; the room then burns at its
  ventilation/fuel limited rate until 70 % of the fuel load is consumed,
  after which it decays linearly (Eurocode 1 design-fire shape).
* Smoke filling: plume entrainment (Heskestad) fills a hot ceiling layer that
  descends. Once below the door soffit it spills into connected rooms with a
  buoyancy-driven door flow, carrying heat, soot, CO and CO2 with it. The
  door jet entrains room air on the way, so smoke gets cooler and thinner
  with every doorway it passes.
* Doors: each scenario has some doors open and the rest closed; a closed
  door only leaks (CLOSED_DOOR_LEAKAGE of its area) and holds back flames
  for much longer.
* Corridors are split into cells along their length (topology.py), so smoke
  travels down a corridor instead of reaching every door at once.
* Stairs: hot smoke rises through stairwells to the floor above (buoyant
  stack flow), and from there spreads on that floor.
* Smoke removal: background ventilation (VENTILATION_ACH), soot settling on
  surfaces, and a smoke-purge damper per zone that the building agent opens
  (PURGE_ACH air changes per hour when fully open).
* Species: soot, CO and CO2 yields per kg of fuel; CO and soot yields rise
  sharply when the fire becomes under-ventilated (global equivalence ratio).
  Smoke is converted to optical obscuration with the specific extinction
  coefficient (8.7 m2/g flaming, 4.4 m2/g smouldering).
* Spread: a neighbouring room ignites when its hot layer gets hot enough to
  ignite its contents, or when flames have projected through the shared door
  for a while after flashover.
* Sprinklers: heat release decays as exp(-t / 43 s) once water is applied
  (NIST, Madrzykowski & Vettori), much less effectively after flashover;
  sprinkler spray also cools the gas. Sprinklers do NOT remove smoke.
  Switching the sprinkler off before the fire is out lets it regrow.
* Nuisance sources (cooking, steam, dust, heaters, smoking) produce smoke,
  heat or CO without a fire, so a detector has realistic false-alarm cases
  to tell apart from real fires.

Scenarios (see scenarios.py):

* SCENARIO_MODE=fixed  (default) one scenario from the environment variables,
  running forever, as before.
* SCENARIO_MODE=random an endless series of randomised scenarios (fires,
  nuisances, sensor faults, quiet periods) for collecting training data.
  Ground-truth labels (fire_phase, fire_origin, nuisance_source,
  sensor_fault, scenario_id, scenario_time) go to sim/truth/... for the
  database; the scenario (including its sensor-fault schedule, which the
  sensor gateways apply) is published on sim/scenario.
"""
import json
import math
import os
import random
import time

import common
import lifecycle
import mqtt_bus
import scenarios
import simclock
import topology as topology_lib


def env_float(name, default):
    return float(os.environ.get(name, str(default)))


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# Real seconds between published snapshots.
INTERVAL = env_float("FIRE_TICK_SECONDS", 2.0)

# Simulated seconds per real second (shared with the sensors via common).
TIME_SCALE = common.TIME_SCALE

# Internal integration step in simulated seconds.
SUBSTEP_S = env_float("FIRE_SUBSTEP_SECONDS", 0.5)


# ---------------------------------------------------------------------------
# Rooms and connections
# ---------------------------------------------------------------------------

# The whole building: every room and corridor on every floor, with the
# doorways between them derived from BuildSim's walkable graph (topology.py).
# Loaded on first use (from BuildSim, or BUILDING_DATA_DIR for offline work).
_TOPOLOGY = None


def get_topology():
    global _TOPOLOGY
    if _TOPOLOGY is None:
        _TOPOLOGY = topology_lib.Topology.load(common.BASE)
    return _TOPOLOGY


def set_topology(topo):
    global _TOPOLOGY
    _TOPOLOGY = topo


# ---------------------------------------------------------------------------
# Fire scenario
# ---------------------------------------------------------------------------

# fixed: one scenario from the environment variables (START_FIRE_ROOMS,
#        FIRE_START_DELAY_SECONDS, INCIPIENT_SECONDS, FIRE_GROWTH,
#        FUEL_LOAD_MJ_PER_M2, EXTERNAL_DOORS, VENTILATION_ACH).
# random: endless randomised scenarios for data collection.
SCENARIO_MODE = os.environ.get("SCENARIO_MODE", "fixed").strip().lower()

# JSON-lines file that receives every scenario's parameters ("" = off).
SCENARIO_LOG = os.environ.get("SCENARIO_LOG", "")

# Heat release rate when flaming starts (kW). A small item such as a
# waste-paper basket is roughly 10-50 kW.
FLAMING_START_HRR_KW = env_float("FLAMING_START_HRR_KW", 20.0)

# Office fuel: Eurocode 1 (EN 1991-1-2) heat release per floor area.
HRR_PER_AREA_KW_M2 = env_float("HRR_PER_AREA_KW_M2", 250.0)

# Fuel chemistry: mixed cellulosic/plastic office contents.
HEAT_OF_COMBUSTION_KJ_G = env_float("HEAT_OF_COMBUSTION_MJ_KG", 20.0)
SOOT_YIELD = env_float("SOOT_YIELD", 0.05)
CO_YIELD_WELL_VENTILATED = env_float("CO_YIELD", 0.004)
CO_YIELD_UNDER_VENTILATED = env_float("CO_YIELD_UNDERVENTILATED", 0.20)
CO2_YIELD = env_float("CO2_YIELD", 1.5)

# Smouldering combustion: low heat, a lot of smoke and CO.
SMOULDER_MASS_RATE_G_S = env_float("SMOULDER_MASS_RATE_G_S", 0.3)
SMOULDER_HEAT_KJ_G = 10.0
SMOULDER_SOOT_YIELD = 0.10
SMOULDER_CO_YIELD = 0.10
SMOULDER_CO2_YIELD = 1.0

# Specific extinction coefficients (m2/g) for flaming and smouldering smoke.
SIGMA_FLAMING = 8.7
SIGMA_SMOULDER = 4.4

# Flashover and spread.
FLASHOVER_TEMP_C = env_float("FLASHOVER_TEMP_C", 600.0)
# Hot-layer temperature in a neighbouring room that ignites its contents
# (~20 kW/m2 radiation to the floor).
SPREAD_IGNITION_TEMP_C = env_float("SPREAD_IGNITION_TEMP_C", 500.0)
# Simulated seconds of flames projecting through a door after flashover
# before the room on the other side ignites (randomised +/- 30 %).
FLAME_SPREAD_DELAY_S = env_float("FLAME_SPREAD_DELAY_SECONDS", 90.0)

# Sprinklers.
SPRINKLER_DECAY_TIME_S = env_float("SPRINKLER_DECAY_TIME_SECONDS", 43.0)
# Relative effectiveness against a fully developed (post-flashover) fire.
SPRINKLER_FD_EFFECTIVENESS = env_float("SPRINKLER_FD_EFFECTIVENESS", 0.25)
# Fraction of the gas temperature rise removed by the water spray.
SPRINKLER_GAS_COOLING = env_float("SPRINKLER_GAS_COOLING", 0.6)
# Below this heat release rate a suppressed fire is out.
EXTINGUISH_HRR_KW = env_float("EXTINGUISH_HRR_KW", 2.0)

# Smoke control: air changes per hour of a fully open smoke-purge damper.
PURGE_ACH = env_float("PURGE_ACH", 10.0)
# Soot settling on walls and surfaces (time constant, s). Gases do not settle.
SOOT_DEPOSITION_TAU_S = env_float("SOOT_DEPOSITION_SECONDS", 1800.0)

# Doors and stairs.
# Leakage of a closed door, as a fraction of the open door's area.
CLOSED_DOOR_LEAKAGE = env_float("CLOSED_DOOR_LEAKAGE", 0.01)
# A closed door holds flames back this many times longer than an open one.
CLOSED_DOOR_SPREAD_FACTOR = env_float("CLOSED_DOOR_SPREAD_FACTOR", 6.0)
# Room air entrained by the smoke jet through a door / corridor opening /
# stairwell, per unit of smoke volume.
DOOR_JET_ENTRAINMENT = env_float("DOOR_JET_ENTRAINMENT", 1.0)
OPENING_ENTRAINMENT = env_float("OPENING_ENTRAINMENT", 0.3)
STAIR_ENTRAINMENT = env_float("STAIR_ENTRAINMENT", 1.0)
# Effective flow area of a stairwell between two floors (m2) and the storey
# height that drives the stack flow (m).
STAIR_FLOW_AREA_M2 = env_float("STAIR_FLOW_AREA_M2", 0.8)
FLOOR_HEIGHT_M = env_float("FLOOR_HEIGHT_M", 3.5)
STAIR_MIXING_M3S = env_float("STAIR_MIXING_M3S", 0.02)

# Horizontal distance from the fire to the ceiling detectors (m).
DETECTOR_RADIUS_M = env_float("DETECTOR_RADIAL_DISTANCE_M", 2.5)

# Floor-plan units per metre, used only to size the visual effects.
PLAN_UNITS_PER_M = env_float("PLAN_UNITS_PER_M", 5.0)

RANDOM_SEED = os.environ.get("RANDOM_SEED")
RNG = random.Random(int(RANDOM_SEED)) if RANDOM_SEED else random.Random()


# ---------------------------------------------------------------------------
# Physical constants and helpers
# ---------------------------------------------------------------------------

G = 9.81
T_AMB_C = common.AMBIENT_TEMP_C
T_AMB_K = T_AMB_C + 273.15
DOOR_CD = 0.7          # door discharge coefficient
DOOR_MIXING_M3S = 0.02  # turbulent exchange through an open door
MAX_GAS_TEMP_C = 1200.0

# Lagged response of the gas temperature to the fire (s).
TAU_FIRE_ROOM_S = 30.0
TAU_SPRINKLER_S = 10.0
TAU_WALL_LOSS_S = 180.0
TAU_LAYER_MIXING_S = 120.0

# Gypsum board lining, for the MQH wall heat-transfer coefficient.
H_K_STEADY = 0.48 / 0.013 / 1000.0   # k/delta, kW/m2K
T_PENETRATION_S = 106.0


def clamp(value, minimum=0.0, maximum=1.0):
    return min(max(value, minimum), maximum)


def ppm(concentration_g_m3, molar_mass, temp_c):
    """Mass concentration (g/m3) -> volume fraction in ppm."""
    mol_per_m3 = 101325.0 / (8.314 * (temp_c + 273.15))
    return concentration_g_m3 / molar_mass / mol_per_m3 * 1e6


def alpert_excess_temp(q_kw, height, radius):
    """Alpert ceiling-jet temperature rise above ambient (K)."""
    if q_kw <= 0:
        return 0.0
    if radius / height > 0.18:
        return 5.38 * (q_kw / radius) ** (2.0 / 3.0) / height
    return 16.9 * q_kw ** (2.0 / 3.0) / height ** (5.0 / 3.0)


def alpert_velocity(q_kw, height, radius):
    """Alpert ceiling-jet velocity (m/s)."""
    if q_kw <= 0:
        return 0.0
    if radius / height > 0.15:
        return 0.197 * q_kw ** (1.0 / 3.0) * height ** 0.5 / radius ** (5.0 / 6.0)
    return 0.947 * (q_kw / height) ** (1.0 / 3.0)


# ---------------------------------------------------------------------------
# Room model
# ---------------------------------------------------------------------------

IDLE = "idle"
SMOULDERING = "smouldering"
GROWING = "growing"
FULLY_DEVELOPED = "fully_developed"
DECAYING = "decaying"
EXTINGUISHED = "extinguished"
BURNT_OUT = "burnt_out"

BURNING_PHASES = (SMOULDERING, GROWING, FULLY_DEVELOPED, DECAYING)


class Room:
    def __init__(self, zone, params, openings, spread=None):
        """zone: topology.Zone (or one cell of a corridor); params: scenario
        dict (alpha, fuel_load, external_doors, ach); openings: [(other cell
        key, effective width, height)]; spread: {neighbour: factor on the
        flame-spread delay} (closed doors hold flames back longer)."""
        self.name = zone.key
        self.zone_key = zone.key
        self.level, self.room = zone.level, zone.name
        self.zone_type = zone.type
        self.center = zone.center
        self.area = zone.area
        self.height = zone.height
        self.volume = self.area * self.height
        self.spread_factor = dict(spread) if spread is not None else {
            other: 1.0 for other, _, _ in openings}
        self.neighbours = list(self.spread_factor)

        # Openings: the doorways to neighbouring zones, plus open windows in
        # rooms (scenario "external_doors"), plus leakage so a sealed room
        # still gets a little air.
        self.external_doors = int(params["external_doors"]) if zone.type == "room" else 0
        self.alpha = params["alpha"]
        self.ach = params["ach"]
        self.incipient = 0.0
        self.openings = {other: (w, h) for other, w, h in openings}
        self.q_fuel = HRR_PER_AREA_KW_M2 * self.area
        self.update_ventilation()

        self.fuel_total_mj = params["fuel_load"] * self.area
        self.fuel_used_mj = 0.0

        self.phase = IDLE
        self.hrr = 0.0
        self.mass_rate = 0.0  # g/s
        self.phase_time = 0.0
        self.flaming_time = 0.0
        self.fd_time = 0.0
        self.decay_hrr0 = 0.0
        self.smoulder_rate = 0.0

        # Hot layer.
        self.temp = T_AMB_C
        self.layer_volume = 0.0
        self.soot_g = 0.0
        self.extinction_m2 = 0.0  # sum(sigma * soot mass)
        self.co_g = 0.0
        self.co2_g = 0.0  # excess over background

        self.sprinkler_on = False
        self.damper = 0.0
        self.spread_delay = {}

        # Heat from active nuisance sources (kW), not from the fire.
        self.extra_heat = 0.0

    def update_ventilation(self):
        """Opening factor, ventilation limit and surface from the current
        openings (called again when a door opens or closes)."""
        door_w, door_h = common.DOOR_WIDTH_M, common.DOOR_HEIGHT_M
        openings = list(self.openings.values())
        areas = [w * h for w, h in openings] + [door_w * door_h] * self.external_doors
        self.vent_factor = sum(w * h * math.sqrt(h) for w, h in openings) + (
            self.external_doors * door_w * door_h * math.sqrt(door_h))
        self.vent_factor = max(self.vent_factor, 0.1 * door_w * door_h * math.sqrt(door_h))
        self.q_vent = 1500.0 * self.vent_factor
        self.q_max = min(self.q_vent, self.q_fuel)
        # Internal surface area (walls + floor + ceiling - openings).
        self.surface = max(
            2 * self.area + 4 * math.sqrt(self.area) * self.height - sum(areas),
            self.area,
        )
        # Thomas' flashover criterion, used as the "no longer flashed over"
        # threshold when a fully developed fire is being suppressed.
        self.q_flashover = 7.8 * self.surface + 378.0 * self.vent_factor

    def set_opening(self, other, width, height):
        if self.openings.get(other) != (width, height):
            self.openings[other] = (width, height)
            self.update_ventilation()

    # -- derived quantities -------------------------------------------------

    def is_quiet(self):
        """Back at ambient with nothing going on: can leave the active set."""
        if self.burning or self.extra_heat > 0 or self.sprinkler_on:
            return False
        # Thresholds well below what any sensor can resolve.
        v = self.layer_effective_volume
        return (
            abs(self.temp - T_AMB_C) < 0.05
            and self.extinction_m2 / v < 5e-4
            and ppm(self.co_g / v, 28.0, self.temp) < 0.5
            and ppm(self.co2_g / v, 44.0, self.temp) < 10.0
        )

    def reset_layer(self):
        self.temp = T_AMB_C
        self.layer_volume = 0.0
        self.soot_g = self.extinction_m2 = self.co_g = self.co2_g = 0.0

    @property
    def burning(self):
        return self.phase in BURNING_PHASES and (
            self.hrr > 0 or self.phase == SMOULDERING
        )

    @property
    def layer_effective_volume(self):
        return max(self.layer_volume, 0.02 * self.volume)

    @property
    def interface_height(self):
        return clamp(
            self.height - self.layer_volume / self.area, 0.0, self.height
        )

    @property
    def extinction_coefficient(self):
        return self.extinction_m2 / self.layer_effective_volume

    def equivalence_ratio(self):
        demand = self.q_fuel if self.phase == FULLY_DEVELOPED else self.hrr
        return demand / self.q_vent if self.q_vent > 0 else 0.0

    @property
    def total_heat(self):
        """Heat input to the room from the fire and nuisance sources (kW)."""
        return self.hrr + self.extra_heat

    def mqh_temperature(self):
        """MQH steady hot-layer temperature for the current heat (degC)."""
        q = self.total_heat
        if q <= 0:
            return T_AMB_C
        if self.hrr > 0:
            h_k = H_K_STEADY * max(
                1.0, math.sqrt(T_PENETRATION_S / max(self.flaming_time, 1.0))
            )
        else:
            h_k = H_K_STEADY
        delta = 6.85 * (
            q ** 2 / (self.vent_factor * h_k * self.surface)
        ) ** (1.0 / 3.0)
        return min(T_AMB_C + delta, MAX_GAS_TEMP_C)

    # -- ignition -----------------------------------------------------------

    def ignite(self, incipient=0.0):
        """Start a fire: smouldering for `incipient` seconds, then flaming."""
        if self.fuel_used_mj >= self.fuel_total_mj:
            return
        self.incipient = incipient
        if incipient > 0:
            self.phase = SMOULDERING
            self.hrr = 0.0
        else:
            self.phase = GROWING
            self.hrr = FLAMING_START_HRR_KW
        self.phase_time = 0.0
        self.flaming_time = 0.0
        self.fd_time = 0.0
        self.spread_delay = {
            other: FLAME_SPREAD_DELAY_S * factor * RNG.uniform(0.7, 1.3)
            for other, factor in self.spread_factor.items()
        }

    def set_phase(self, phase):
        if phase != self.phase:
            print(
                f"{self.name}: {self.phase} -> {phase} "
                f"(HRR={self.hrr:.0f} kW, T={self.temp:.0f} degC)",
                flush=True,
            )
            self.phase = phase
            self.phase_time = 0.0

    # -- heat release -------------------------------------------------------

    def step_fire(self, dt):
        self.phase_time += dt

        if self.phase == SMOULDERING:
            if self.sprinkler_on:
                self.smoulder_rate *= math.exp(-dt / SPRINKLER_DECAY_TIME_S)
                if self.smoulder_rate < 0.01:
                    self.smoulder_rate = 0.0
                    self.set_phase(EXTINGUISHED)
            else:
                self.smoulder_rate = SMOULDER_MASS_RATE_G_S * clamp(
                    self.phase_time / self.incipient, 0.05, 1.0
                )
                if self.phase_time >= self.incipient:
                    self.smoulder_rate = 0.0
                    self.hrr = FLAMING_START_HRR_KW
                    self.set_phase(GROWING)
            if self.phase == SMOULDERING:
                self.mass_rate = self.smoulder_rate
                self.hrr = self.smoulder_rate * SMOULDER_HEAT_KJ_G
                return

        if self.phase not in (GROWING, FULLY_DEVELOPED, DECAYING):
            self.hrr = 0.0
            self.mass_rate = 0.0
            return

        self.flaming_time += dt
        suppression = math.exp(-dt / SPRINKLER_DECAY_TIME_S)

        if self.phase == GROWING:
            if self.sprinkler_on:
                self.hrr *= suppression
            else:
                # dQ/dt of Q = alpha t^2, so growth resumes from any Q.
                self.hrr += 2.0 * math.sqrt(self.alpha * self.hrr) * dt
                self.hrr = min(self.hrr, self.q_max)

        elif self.phase == FULLY_DEVELOPED:
            self.fd_time += dt
            if self.sprinkler_on:
                self.hrr *= math.exp(
                    -dt * SPRINKLER_FD_EFFECTIVENESS / SPRINKLER_DECAY_TIME_S
                )
                if self.hrr < 0.5 * self.q_flashover:
                    self.set_phase(GROWING)
            else:
                self.hrr += (self.q_max - self.hrr) * dt / 15.0

        elif self.phase == DECAYING:
            if self.sprinkler_on:
                self.decay_hrr0 *= suppression
            remaining = self.fuel_total_mj - self.fuel_used_mj
            self.hrr = self.decay_hrr0 * clamp(
                remaining / (0.3 * self.fuel_total_mj)
            )

        self.fuel_used_mj += self.hrr * dt / 1000.0

        if self.fuel_used_mj >= self.fuel_total_mj:
            self.hrr = 0.0
            self.set_phase(BURNT_OUT)
        elif (
            self.phase in (GROWING, FULLY_DEVELOPED)
            and self.fuel_used_mj >= 0.7 * self.fuel_total_mj
        ):
            self.decay_hrr0 = self.hrr
            self.set_phase(DECAYING)
        elif (
            self.sprinkler_on or self.phase == DECAYING
        ) and self.hrr < EXTINGUISH_HRR_KW:
            self.hrr = 0.0
            self.set_phase(EXTINGUISHED)

        self.mass_rate = self.hrr / HEAT_OF_COMBUSTION_KJ_G

    # -- smoke production and plume -----------------------------------------

    def step_production(self, dt):
        if self.mass_rate <= 0:
            return

        if self.phase == SMOULDERING:
            soot_yield = SMOULDER_SOOT_YIELD
            co_yield = SMOULDER_CO_YIELD
            co2_yield = SMOULDER_CO2_YIELD
            sigma = SIGMA_SMOULDER
            convective = self.hrr * 0.7 + 1.0
        else:
            under = clamp((self.equivalence_ratio() - 0.5) / 0.7)
            soot_yield = SOOT_YIELD * (1.0 + 1.5 * under)
            co_yield = CO_YIELD_WELL_VENTILATED + (
                CO_YIELD_UNDER_VENTILATED - CO_YIELD_WELL_VENTILATED
            ) * under
            co2_yield = CO2_YIELD * (1.0 - 0.35 * under)
            sigma = SIGMA_FLAMING
            convective = 0.7 * self.hrr

        fuel = self.mass_rate * dt
        self.soot_g += fuel * soot_yield
        self.extinction_m2 += fuel * soot_yield * sigma
        self.co_g += fuel * co_yield
        self.co2_g += fuel * co2_yield

        self.entrain(convective, dt)

    def entrain(self, convective, dt):
        """Heskestad plume entrainment up to the interface fills the layer."""
        z = max(self.interface_height, 0.3)
        plume_kg_s = (
            0.071 * convective ** (1.0 / 3.0) * z ** (5.0 / 3.0)
            + 0.0018 * convective
        )
        # The plume cannot draw in more air than the openings supply
        # (Kawagoe: ~0.5 * A_o * sqrt(H_o) kg/s), which is what makes a
        # post-flashover fire ventilation controlled.
        plume_kg_s = min(plume_kg_s, 0.5 * self.vent_factor)
        rho_layer = 353.0 / (self.temp + 273.15)
        added = plume_kg_s / rho_layer * dt
        overflow = self.layer_volume + added - 0.98 * self.volume
        if overflow > 0:
            # The room is full of smoke: the excess is pushed out through
            # gaps and openings, carrying combustion products with it.
            self.remove_layer_fraction(overflow / (0.98 * self.volume + added))
        self.layer_volume = min(self.layer_volume + added, 0.98 * self.volume)

    def step_nuisances(self, dt, active):
        """Add smoke/heat/CO from nuisance sources (no fire involved)."""
        self.extra_heat = 0.0
        convective = 0.0
        for nuisance in active:
            source = scenarios.NUISANCE_SOURCES[nuisance["type"]]
            k = nuisance["strength"]
            aerosol = source["aerosol"] * k * dt
            self.soot_g += aerosol
            self.extinction_m2 += aerosol * source["sigma"]
            self.co_g += source["co"] * k * dt
            self.co2_g += source["co2"] * k * dt
            self.extra_heat += source["heat"] * k
            convective += 0.7 * source["heat"] * k + (0.5 if aerosol > 0 else 0.0)
        if convective > 0:
            self.entrain(convective, dt)

    # -- layer bookkeeping --------------------------------------------------

    def remove_layer_fraction(self, fraction):
        fraction = clamp(fraction)
        removed = (
            self.soot_g * fraction,
            self.extinction_m2 * fraction,
            self.co_g * fraction,
            self.co2_g * fraction,
        )
        self.soot_g -= removed[0]
        self.extinction_m2 -= removed[1]
        self.co_g -= removed[2]
        self.co2_g -= removed[3]
        return removed

    def add_layer_gas(self, volume, temp, species):
        if volume <= 0:
            return
        total = self.layer_volume + volume
        self.temp = (self.temp * self.layer_volume + temp * volume) / total
        self.layer_volume = min(total, 0.98 * self.volume)
        self.soot_g += species[0]
        self.extinction_m2 += species[1]
        self.co_g += species[2]
        self.co2_g += species[3]

    def step_ventilation(self, dt):
        # Background ventilation plus the smoke-purge damper extract from
        # the ceiling layer; fresh air replaces it from below.
        flow = (self.ach + self.damper * PURGE_ACH) * self.volume / 3600.0
        volume = flow * dt
        self.remove_layer_fraction(volume / self.layer_effective_volume)
        self.layer_volume = max(0.0, self.layer_volume - volume)

        # Soot settles on walls, ceiling and furniture.
        settle = 1.0 - math.exp(-dt / SOOT_DEPOSITION_TAU_S)
        self.soot_g -= self.soot_g * settle
        self.extinction_m2 -= self.extinction_m2 * settle

        # Once the fire is out and the smoke has cooled, it loses its
        # buoyancy and mixes down through the room.
        if (
            not self.burning
            and self.extra_heat <= 0
            and self.temp - T_AMB_C < 15.0
        ):
            self.layer_volume += (
                0.98 * self.volume - self.layer_volume
            ) * dt / TAU_LAYER_MIXING_S

    def step_temperature(self, dt):
        if self.total_heat > 0:
            target = self.mqh_temperature()
            tau = TAU_FIRE_ROOM_S
        else:
            target = T_AMB_C
            tau = TAU_WALL_LOSS_S

        if self.sprinkler_on:
            target = T_AMB_C + (target - T_AMB_C) * (1 - SPRINKLER_GAS_COOLING)
            tau = min(tau, TAU_SPRINKLER_S)

        self.temp += (target - self.temp) * min(1.0, dt / tau)
        self.temp = min(self.temp, MAX_GAS_TEMP_C)

    # -- outputs ------------------------------------------------------------

    def detector_temperature(self):
        """Gas temperature at a ceiling detector DETECTOR_RADIUS_M away."""
        # The detector sees whichever is hotter: the ceiling jet running
        # under the ceiling from the plume, or the hot layer around it.
        jet = alpert_excess_temp(self.total_heat, self.height, DETECTOR_RADIUS_M)
        if self.sprinkler_on:
            jet *= 1 - SPRINKLER_GAS_COOLING
        layer = self.temp if self.layer_volume > 0.01 * self.volume else T_AMB_C
        return min(max(layer, T_AMB_C + jet), MAX_GAS_TEMP_C)

    def detector_velocity(self):
        jet = alpert_velocity(self.total_heat, self.height, DETECTOR_RADIUS_M)
        buoyant = 0.3 * math.sqrt(
            G * self.height * max(self.temp - T_AMB_C, 0.0) / T_AMB_K
        )
        return max(0.1, jet, buoyant)

    def outputs(self):
        k = self.extinction_coefficient
        concentration = 1.0 / self.layer_effective_volume
        co_ppm = ppm(self.co_g * concentration, 28.0, self.temp)
        co2_excess = ppm(self.co2_g * concentration, 44.0, self.temp)
        o2 = clamp(
            common.AMBIENT_O2_PCT - (co2_excess + co_ppm) / 1e4,
            0.0,
            common.AMBIENT_O2_PCT,
        )
        return {
            common.LAYER_HRR: self.hrr,
            common.LAYER_GAS_TEMP: self.detector_temperature(),
            common.LAYER_JET_VELOCITY: self.detector_velocity(),
            common.LAYER_OBSCURATION: 100.0 * (1.0 - math.exp(-k)),
            common.LAYER_VISIBILITY: min(30.0, 3.0 / k) if k > 1e-6 else 30.0,
            common.LAYER_CO: co_ppm,
            common.LAYER_CO2: common.AMBIENT_CO2_PPM + co2_excess,
            common.LAYER_O2: o2,
            common.LAYER_SMOKE_LAYER_HEIGHT: self.interface_height,
        }


# ---------------------------------------------------------------------------
# Building model
# ---------------------------------------------------------------------------

PHASE_SEVERITY = [FULLY_DEVELOPED, GROWING, DECAYING, SMOULDERING, BURNT_OUT,
                  EXTINGUISHED, IDLE]


class ZoneView:
    """One zone (room or whole corridor) as sensors, actuators and the agent
    see it. A corridor is several cells in the physics; the view reports the
    worst cell (a detector near the smoke) and drives all of its cells'
    sprinklers and dampers together."""

    def __init__(self, zone, cells):
        self.zone = zone
        self.cells = cells
        self.name = zone.key
        self.level, self.room = zone.level, zone.name
        self.zone_type = zone.type
        self.area = zone.area

    @property
    def sprinkler_on(self):
        return any(c.sprinkler_on for c in self.cells)

    @sprinkler_on.setter
    def sprinkler_on(self, value):
        for c in self.cells:
            c.sprinkler_on = bool(value)

    @property
    def damper(self):
        return max(c.damper for c in self.cells)

    @damper.setter
    def damper(self, value):
        for c in self.cells:
            c.damper = clamp(float(value))

    @property
    def phase(self):
        return min((c.phase for c in self.cells), key=PHASE_SEVERITY.index)

    @property
    def hrr(self):
        return sum(c.hrr for c in self.cells)

    @property
    def burning(self):
        return any(c.burning for c in self.cells)

    def outputs(self):
        if len(self.cells) == 1:
            return self.cells[0].outputs()
        outs = [c.outputs() for c in self.cells]
        result = {}
        for k in outs[0]:
            values = [o[k] for o in outs]
            if k == common.LAYER_HRR:
                result[k] = sum(values)
            elif k in (common.LAYER_VISIBILITY, common.LAYER_O2,
                       common.LAYER_SMOKE_LAYER_HEIGHT):
                result[k] = min(values)
            else:
                result[k] = max(values)
        return result


class FireModel:
    """The building's zones, stepped with an *active set*: only cells with a
    fire, a nuisance source, smoke or heat (and their neighbours as smoke
    reaches them) are integrated. A quiet building costs almost nothing.

    self.cells  physics cells (a room is one cell, a corridor one or more)
    self.rooms  {zone key: ZoneView}, what sensors and actuators work with
    self.links  [(cell a, cell b, width, height, open fraction, kind)] with
                kind "door", "opening" (corridor to corridor, wide) or
                "stair" (a lower floor's cell a to the floor above's cell b)
    """

    def __init__(self, scenario=None, topo=None):
        self.topology = topo = topo or get_topology()
        self.scenario = scenario or scenarios.fixed_scenario()

        # Cells: rooms and short corridors are one cell; split corridors one
        # per segment, keyed "<zone key>#<i>".
        self.zone_cells = {}
        cell_zones = {}
        for k, zone in topo.zones.items():
            parts = topo.cells.get(k)
            if not parts:
                self.zone_cells[k] = [k]
                cell_zones[k] = zone
                continue
            keys = []
            for i, part in enumerate(parts):
                cell = topology_lib.Zone(zone.level, zone.name, zone.type,
                                         max(1.0, zone.area * part["share"]),
                                         zone.height, part["center"])
                cell.key = f"{k}#{i}"
                keys.append(cell.key)
                cell_zones[cell.key] = cell
            self.zone_cells[k] = keys

        def cell_of(zone_key, index):
            keys = self.zone_cells[zone_key]
            return keys[min(index, len(keys) - 1)]

        self.links = []
        self.door_open = {}
        self.door_links = {}     # connection pair -> link index (doorways with a door)
        for a, b, w, h in topo.connections:
            ia, ib = topo.conn_cells.get((a, b), (0, 0))
            corridors = topo.zones[a].type == "corridor" and topo.zones[b].type == "corridor"
            if corridors:
                kind, fraction = "opening", 1.0
            else:
                is_open = scenarios.door_is_open(self.scenario, a, b)
                self.door_open[(a, b)] = is_open
                kind, fraction = "door", 1.0 if is_open else CLOSED_DOOR_LEAKAGE
                self.door_links[(a, b)] = len(self.links)
            self.links.append((cell_of(a, ia), cell_of(b, ib), w, h, fraction, kind))
        for zone_key, i, j, w, h in topo.cell_links:
            self.links.append((cell_of(zone_key, i), cell_of(zone_key, j), w, h, 1.0, "opening"))
        level_order = {lv: n for n, lv in enumerate(sorted({z.level for z in topo.zones.values()}))}
        for a, b in topo.stairs:
            lower, upper = (a, b) if level_order[topo.zones[a].level] <= \
                level_order[topo.zones[b].level] else (b, a)
            self.links.append((self.zone_cells[lower][0], self.zone_cells[upper][0],
                               0.0, FLOOR_HEIGHT_M, 1.0, "stair"))

        openings = {k: [] for k in cell_zones}
        spread = {k: {} for k in cell_zones}
        for a, b, w, h, fraction, kind in self.links:
            if kind == "stair":
                continue
            openings[a].append((b, w * fraction, h))
            openings[b].append((a, w * fraction, h))
            factor = 1.0 if fraction >= 1.0 else CLOSED_DOOR_SPREAD_FACTOR
            spread[a][b] = spread[b][a] = factor
        self.cells = {
            k: Room(zone, self.scenario, openings[k], spread[k])
            for k, zone in cell_zones.items()
        }
        for zone_key, keys in self.zone_cells.items():
            for k in keys:
                self.cells[k].zone_key = zone_key
        self.rooms = {
            k: ZoneView(zone, [self.cells[c] for c in self.zone_cells[k]])
            for k, zone in topo.zones.items()
        }
        self.link_fraction = [link[4] for link in self.links]
        self.links_by_cell = {k: [] for k in self.cells}
        for i, (a, b, *_rest) in enumerate(self.links):
            self.links_by_cell[a].append(i)
            self.links_by_cell[b].append(i)
        self.active = set()   # cell keys
        self.time = 0.0
        self.pending_fires = sorted(
            (f for f in self.scenario["fires"] if f["room"] in self.rooms),
            key=lambda f: f["start"],
        )
        self.origins = set()

    def set_doors(self, open_by_pair):
        """Open or close doors: {(zone a, zone b): True/False}. Returns how
        many changed. Smoke flows, ventilation limits and the opening factor
        of the two cells follow at once."""
        changed = 0
        for pair, is_open in open_by_pair.items():
            i = self.door_links.get(pair)
            if i is None or self.door_open.get(pair) == is_open:
                continue
            self.door_open[pair] = is_open
            fraction = 1.0 if is_open else CLOSED_DOOR_LEAKAGE
            self.link_fraction[i] = fraction
            a, b, w, h, _, _ = self.links[i]
            self.cells[a].set_opening(b, w * fraction, h)
            self.cells[b].set_opening(a, w * fraction, h)
            # Fresh air (or smoke) can move again: both sides are active.
            self.active.update((a, b))
            changed += 1
        return changed

    def primary_cell(self, zone_key):
        """The cell where a zone's fire or nuisance source sits (the middle
        of a corridor)."""
        keys = self.zone_cells[zone_key]
        return keys[len(keys) // 2]

    @property
    def active_zones(self):
        return {self.cells[c].zone_key for c in self.active}

    @property
    def finished(self):
        return self.time >= self.scenario["duration"]

    def labels(self, name):
        """Ground-truth labels for one zone (numeric codes)."""
        room = self.rooms[name]
        active = scenarios.active_nuisances(self.scenario, name, self.time)
        return {
            common.LAYER_FIRE_PHASE: scenarios.PHASE_CODES[room.phase],
            common.LAYER_FIRE_ORIGIN: 1 if name in self.origins else 0,
            common.LAYER_NUISANCE: (
                scenarios.NUISANCE_CODES[active[0]["type"]] if active else 0
            ),
            common.LAYER_SENSOR_FAULT: scenarios.active_fault(
                self.scenario, name, self.time
            ),
            common.LAYER_SCENARIO_ID: self.scenario["id"],
            common.LAYER_SCENARIO_TIME: self.time,
        }

    def fault_zones(self):
        return {f["room"] for f in self.scenario["sensor_faults"]
                if f["room"] in self.rooms and self.time >= f["start"]}

    def step(self, dt):
        """Advance the whole building by dt simulated seconds."""
        steps = max(1, int(math.ceil(dt / SUBSTEP_S)))
        h = dt / steps
        for _ in range(steps):
            self._substep(h)

    def _substep(self, dt):
        self.time += dt

        while self.pending_fires and self.time >= self.pending_fires[0]["start"]:
            fire = self.pending_fires.pop(0)
            print(f"{fire['room']}: fire starts", flush=True)
            self.origins.add(fire["room"])
            cell = self.primary_cell(fire["room"])
            self.cells[cell].ignite(fire.get("incipient", 0.0))
            self.active.add(cell)
        nuisance_cells = {}
        for nuisance in self.scenario["nuisances"]:
            if nuisance["room"] in self.rooms and \
                    nuisance["start"] <= self.time < nuisance["start"] + nuisance["duration"]:
                cell = self.primary_cell(nuisance["room"])
                nuisance_cells.setdefault(cell, []).append(nuisance)
                self.active.add(cell)
        for name, cell in self.cells.items():
            if cell.sprinkler_on:
                self.active.add(name)

        for name in list(self.active):
            cell = self.cells[name]
            cell.step_fire(dt)
            cell.step_production(dt)
            cell.step_nuisances(dt, nuisance_cells.get(name, ()))

        self._door_flows(dt)

        for name in list(self.active):
            cell = self.cells[name]
            cell.step_ventilation(dt)
            cell.step_temperature(dt)

        self._transitions(dt)

        for name in list(self.active):
            cell = self.cells[name]
            if cell.is_quiet():
                cell.reset_layer()
                self.active.discard(name)

    @staticmethod
    def _transfer(hot, cold, volume, entrainment):
        """Move `volume` of hot layer gas into the cold cell; the jet entrains
        `entrainment` times its volume of the cold cell's lower-layer air."""
        volume = min(volume, 0.2 * hot.layer_volume)
        if volume <= 0:
            return False
        species = hot.remove_layer_fraction(volume / hot.layer_effective_volume)
        hot.layer_volume -= volume
        mixed_temp = (hot.temp + entrainment * T_AMB_C) / (1.0 + entrainment)
        cold.add_layer_gas(volume * (1.0 + entrainment), mixed_temp, species)
        return True

    @staticmethod
    def _mix(a, b, flow_m3s, dt):
        """Turbulent exchange of concentrations between two cells."""
        mix = flow_m3s * dt
        fa = mix / a.layer_effective_volume
        fb = mix / b.layer_effective_volume
        moved = False
        for attr in ("soot_g", "extinction_m2", "co_g", "co2_g"):
            va, vb = getattr(a, attr), getattr(b, attr)
            transfer = va * fa - vb * fb
            if abs(transfer) > 1e-6:
                moved = True
            setattr(a, attr, va - transfer)
            setattr(b, attr, vb + transfer)
        return moved

    def _door_flows(self, dt):
        width = common.DOOR_WIDTH_M
        height = common.DOOR_HEIGHT_M

        # Hot layer escaping through open windows (rooms only).
        for name in self.active:
            room = self.cells[name]
            if room.external_doors <= 0:
                continue
            delta = room.temp - T_AMB_C
            spill = clamp((height - room.interface_height) / (0.5 * height))
            if delta <= 0.1 or spill <= 0:
                continue
            flow = (
                room.external_doors * DOOR_CD / 3.0 * width * height
                * math.sqrt(G * height * delta / T_AMB_K) * spill
            )
            volume = min(flow * dt, 0.2 * room.layer_volume)
            room.remove_layer_fraction(volume / room.layer_effective_volume)
            room.layer_volume -= volume

        indices = set()
        for name in self.active:
            indices.update(self.links_by_cell[name])
        newly_active = set()
        for i in sorted(indices):
            name_a, name_b, w, h, _, kind = self.links[i]
            fraction = self.link_fraction[i]
            a, b = self.cells[name_a], self.cells[name_b]

            if kind == "stair":
                # Stack flow: hot smoke in the lower cell rises up the
                # stairwell to the floor above. It never sinks.
                delta = a.temp - b.temp
                if delta > 0.1 and a.layer_volume > 0.05 * a.volume:
                    flow = DOOR_CD * STAIR_FLOW_AREA_M2 * math.sqrt(
                        2.0 * G * h * delta / (b.temp + 273.15))
                    if self._transfer(a, b, flow * dt, STAIR_ENTRAINMENT):
                        newly_active.add(name_b)
                if self._mix(a, b, STAIR_MIXING_M3S, dt):
                    newly_active.update((name_a, name_b))
                continue

            # Buoyancy-driven hot-layer outflow from the hotter cell.
            hot, cold = (a, b) if a.temp >= b.temp else (b, a)
            delta = hot.temp - cold.temp
            spill = clamp((h - hot.interface_height) / (0.5 * h))
            if delta > 0.1 and spill > 0:
                flow = (
                    DOOR_CD / 3.0 * w * fraction * h
                    * math.sqrt(G * h * delta / (cold.temp + 273.15))
                    * spill
                )
                entrainment = DOOR_JET_ENTRAINMENT if kind == "door" else OPENING_ENTRAINMENT
                if self._transfer(hot, cold, flow * dt, entrainment):
                    newly_active.add(cold.name)

            # Turbulent mixing through the doorway evens out concentrations
            # in both directions (a closed door only through its gaps).
            if self._mix(a, b, DOOR_MIXING_M3S * (w / width) * fraction, dt):
                newly_active.update((name_a, name_b))
        self.active |= newly_active

    def _transitions(self, dt):
        active = [self.cells[n] for n in list(self.active)]
        for room in active:
            if room.phase == GROWING and not room.sprinkler_on and (
                room.temp >= FLASHOVER_TEMP_C
            ):
                room.set_phase(FULLY_DEVELOPED)
                print(f"{room.name}: FLASHOVER", flush=True)

        for room in active:
            if room.phase != FULLY_DEVELOPED:
                continue
            for other_name in room.neighbours:
                other = self.cells[other_name]
                if other.phase not in (IDLE, EXTINGUISHED):
                    continue
                if room.fd_time >= room.spread_delay.get(other_name, 0.0):
                    print(
                        f"{other_name}: IGNITION by flames through the door "
                        f"from {room.name}",
                        flush=True,
                    )
                    other.ignite()
                    self.active.add(other_name)

        for room in active:
            if room.phase in (IDLE, EXTINGUISHED) and (
                room.temp >= SPREAD_IGNITION_TEMP_C
            ):
                print(
                    f"{room.name}: IGNITION by hot smoke layer "
                    f"({room.temp:.0f} degC)",
                    flush=True,
                )
                room.ignite()


# ---------------------------------------------------------------------------
# BuildSim publishing
# ---------------------------------------------------------------------------

HEAT = ["#1e293b", "#7c2d12", "#ea580c", "#facc15", "#fef9c3"]
SMOKE = ["#e2e8f0", "#94a3b8", "#475569", "#0f172a"]
DANGER_HIGH = ["#22c55e", "#facc15", "#f97316", "#dc2626"]
DANGER_LOW = list(reversed(DANGER_HIGH))

LAYER_META = [
    (common.LAYER_HRR, "Fire heat release rate", "kW", 0, 3000, HEAT),
    (common.LAYER_GAS_TEMP, "Ceiling gas temperature", "°C", 20, 800, DANGER_HIGH),
    (common.LAYER_OBSCURATION, "Smoke obscuration", "%/m", 0, 50, SMOKE),
    (common.LAYER_VISIBILITY, "Visibility", "m", 0, 30, DANGER_LOW),
    (common.LAYER_CO, "Carbon monoxide", "ppm", 0, 2000, DANGER_HIGH),
    (common.LAYER_CO2, "Carbon dioxide", "ppm", 400, 20000, DANGER_HIGH),
    (common.LAYER_O2, "Oxygen", "%", 10, 21, DANGER_LOW),
    (common.LAYER_SMOKE_LAYER_HEIGHT, "Smoke layer height", "m", 0,
     common.CEILING_HEIGHT_M, DANGER_LOW),
    (common.LAYER_JET_VELOCITY, "Ceiling jet velocity", "m/s", 0, 3, DANGER_HIGH),
]


def build_layers(model):
    """Room layers for the viewer: only zones that are not at ambient
    (omitted rooms stay uncoloured), so the payload stays small."""
    outputs = {
        common.room_key(view.room, view.level): view.outputs()
        for view in (model.rooms[n] for n in sorted(model.active_zones))
    }
    layers = []
    for layer_id, label, unit, minimum, maximum, palette in LAYER_META:
        layers.append(
            {
                "id": layer_id,
                "label": label,
                "unit": unit,
                "source": "simulation truth",
                "minimum": minimum,
                "maximum": maximum,
                "opacity": 0.7,
                "palette": palette,
                "values": {
                    key: round(values[layer_id], 3)
                    for key, values in outputs.items()
                },
            }
        )
    return layers


def build_effects(model):
    """Convert the physical state into BuildSim visual effects."""

    effects = []
    units = PLAN_UNITS_PER_M

    for name in sorted(model.active):
        room = model.cells[name]
        # A corridor cell is drawn at its own position along the corridor.
        suffix = "" if "#" not in name else "-" + name.rsplit("#", 1)[1]
        place = {"level": room.level, "room": room.room}
        if suffix and room.center:
            place["position"] = [round(room.center[0], 2), round(room.center[1], 2)]
        room_radius = clamp(math.sqrt(room.area / math.pi) * units, 3.0, 40.0)
        ceiling = room.height * units
        out = room.outputs()

        if room.hrr > 0.5:
            if room.phase == FULLY_DEVELOPED:
                radius, height = room_radius, ceiling
            else:
                diameter = math.sqrt(4 * (room.hrr / HRR_PER_AREA_KW_M2) / math.pi)
                flame = max(
                    0.2, 0.235 * room.hrr ** 0.4 - 1.02 * diameter
                )
                radius = clamp(diameter / 2 * units, 1.5, room_radius)
                height = clamp(min(flame, room.height) * units, 1.0, ceiling)
            effects.append(
                {
                    "id": f"fire-{room.level}-{room.room}{suffix}",
                    "type": "fire",
                    "label": f"Fire ({room.phase.replace('_', ' ')}, "
                             f"{room.hrr:.0f} kW)",
                    **place,
                    "radius": round(radius, 2),
                    "height": round(height, 2),
                    "intensity": round(clamp(math.sqrt(room.hrr / 2500.0), 0.05, 1.0), 3),
                }
            )

        if out[common.LAYER_OBSCURATION] > 0.5:
            effects.append(
                {
                    "id": f"smoke-{room.level}-{room.room}{suffix}",
                    "type": "smoke",
                    "label": f"Smoke (visibility {out[common.LAYER_VISIBILITY]:.1f} m)",
                    **place,
                    "radius": round(room_radius, 2),
                    "height": round(ceiling, 2),
                    "intensity": round(
                        clamp(1.0 - math.exp(-room.extinction_coefficient * 3.0),
                              0.02, 1.0), 3
                    ),
                }
            )

        if room.sprinkler_on:
            effects.append(
                {
                    "id": f"sprinkler-{room.level}-{room.room}{suffix}",
                    "type": "sprinkler",
                    "label": "Sprinkler discharging",
                    **place,
                    "radius": round(room_radius * 0.8, 2),
                    "height": round(ceiling, 2),
                    "intensity": 1.0,
                }
            )

    return effects


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

AMBIENT_TRUTH = None
TRUTH_REFRESH_S = 30.0   # real seconds between re-publishing unchanged truth


class BuildSimActuators:
    """Actuator state read back from BuildSim, the single source of truth
    for it (written there by the actuator gateway)."""

    def __init__(self):
        self.states = {}      # (zone key, actuator) -> state string
        self.failures = 0

    def read(self):
        try:
            items = common.call("GET", "/api/equipment?category=fire_suppression") or []
        except Exception as exc:  # noqa: BLE001
            # Keep the last known states: a short BuildSim hiccup must not
            # switch every sprinkler off in the physics.
            self.failures += 1
            if self.failures % 10 == 1:
                print(f"could not read actuator state from BuildSim: {exc}", flush=True)
            return False
        self.failures = 0
        states = {}
        for item in items:
            zone = f"{item.get('level')}/{item.get('room')}"
            for actuator in item.get("actuators") or []:
                kind = str(actuator.get("id", "")).rsplit("-", 1)[-1]
                if kind in ("sprinkler", "damper"):
                    states[(zone, kind)] = str(actuator.get("state"))
        self.states = states
        return True

    def apply(self, model):
        for zone, view in model.rooms.items():
            view.sprinkler_on = self.states.get((zone, "sprinkler")) == "on"
            try:
                view.damper = clamp(float(self.states.get((zone, "damper"), 0.0)))
            except ValueError:
                view.damper = 0.0


class BuildSimDoors:
    """Door states read back from BuildSim (written by the door gateway).
    Without a door gateway BuildSim has no doors and the scenario's door
    states stay in force."""

    def __init__(self, topo):
        self.pairs = {did: pair for did, pair, _, _ in topo.doors()}
        self.failures = 0

    def apply(self, model):
        try:
            doors = common.call("GET", "/api/doors") or []
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            if self.failures % 10 == 1:
                print(f"could not read doors from BuildSim: {exc}", flush=True)
            return 0
        self.failures = 0
        states = {self.pairs[d["id"]]: d.get("state") == "open"
                  for d in doors if d.get("id") in self.pairs}
        changed = model.set_doors(states)
        if changed:
            print(f"{changed} doors changed state", flush=True)
        return changed


class SimBus:
    """Ground truth, scenario and clock out (evaluation and metadata only)."""

    def __init__(self):
        self.life = lifecycle.Lifecycle("fire-simulator", status_topic=mqtt_bus.SIM_STATUS_TOPIC)
        self.bus = self.life.bus
        self.published = {}   # zone key -> (payload signature, real time)

    def start(self):
        self.life.start()
        return self

    def publish_truth(self, model):
        """Retained truth per zone: active zones and zones with a sensor
        fault, plus one last ambient message for zones that went quiet."""
        now = time.monotonic()
        faults = model.fault_zones()
        active = model.active_zones
        targets = active | faults | set(self.published)
        for name in targets:
            room = model.rooms.get(name)
            if room is None:
                continue
            truth = room.outputs()
            truth.update(model.labels(name))
            signature = tuple(round(v, 2) for k, v in sorted(truth.items())
                              if k not in (common.LAYER_SCENARIO_TIME,))
            previous = self.published.get(name)
            quiet = name not in active and name not in faults
            if previous and previous[0] == signature and now - previous[1] < TRUTH_REFRESH_S:
                if quiet:
                    self.published.pop(name, None)   # its ambient truth is already out
                continue
            payload = {k: round(v, 4) for k, v in truth.items()}
            payload["ts"] = time.time()
            self.bus.publish(mqtt_bus.truth_topic(room.level, room.room), payload, qos=0, retain=True)
            self.published[name] = (signature, now)

    def clear_all_truth(self, topo):
        """Remove retained truth left over from a previous run."""
        for zone in topo.zones.values():
            self.bus.clear_retained(mqtt_bus.truth_topic(zone.level, zone.name))

    def publish_clock(self, model, sim_time):
        """The building clock (simclock.py): scenario time, simulated time
        since start and the building's wall clock."""
        body = simclock.message(model.scenario["id"], model.time, sim_time)
        body["active_zones"] = len(model.active_zones)
        self.bus.publish(mqtt_bus.CLOCK_TOPIC, body, qos=0, retain=True)

    def publish_scenario(self, scenario):
        self.bus.publish(mqtt_bus.SCENARIO_TOPIC, scenarios.to_json(scenario), qos=1, retain=True)


def publish_buildsim(model):
    common.set_room_layers(build_layers(model))
    common.set_effects(build_effects(model))


def log(model):
    for name in sorted(model.active_zones):
        room = model.rooms[name]
        out = room.outputs()
        print(
            f"scenario={model.scenario['id']} "
            f"t={model.time:6.0f}s zone={name} phase={room.phase} "
            f"hrr={room.hrr:7.1f}kW gas={out[common.LAYER_GAS_TEMP]:6.1f}C "
            f"obsc={out[common.LAYER_OBSCURATION]:5.1f}%/m "
            f"vis={out[common.LAYER_VISIBILITY]:4.1f}m "
            f"co={out[common.LAYER_CO]:6.0f}ppm "
            f"o2={out[common.LAYER_O2]:4.1f}% "
            f"layer={out[common.LAYER_SMOKE_LAYER_HEIGHT]:.2f}m "
            f"sprinkler={room.sprinkler_on}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def log_scenario(scenario):
    print(f"SCENARIO {json.dumps(scenarios.to_json(scenario))}", flush=True)
    if not SCENARIO_LOG:
        return
    try:
        os.makedirs(os.path.dirname(SCENARIO_LOG) or ".", exist_ok=True)
        with open(SCENARIO_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(scenarios.to_json(scenario)) + "\n")
    except OSError as exc:
        print(f"could not write {SCENARIO_LOG}: {exc}", flush=True)


def scenario_zones(topo):
    """(rooms, corridors) that can host a fire: zones with a doorway."""
    rooms = sorted(k for k, z in topo.zones.items() if z.type == "room" and topo.neighbours[k])
    corridors = sorted(k for k, z in topo.zones.items()
                       if z.type == "corridor" and topo.neighbours[k])
    return rooms, corridors


def new_model(topo, previous_id=None):
    if SCENARIO_MODE == "random":
        # Scenario ids are unique across restarts (seconds since epoch).
        scenario_id = int(time.time())
        if previous_id is not None and scenario_id <= previous_id:
            scenario_id = previous_id + 1
        rooms, corridors = scenario_zones(topo)
        scenario = scenarios.random_scenario(scenario_id, rooms, RNG, corridors=corridors)
    else:
        scenario = scenarios.fixed_scenario()
        unknown = [f["room"] for f in scenario["fires"] if f["room"] not in topo.zones]
        if unknown:
            print(f"WARNING: unknown fire rooms {unknown}", flush=True)
    log_scenario(scenario)
    return FireModel(scenario, topo)


def main():

    common.wait_until_healthy()
    topo = get_topology()
    rooms, corridors = scenario_zones(topo)
    print(
        f"Fire simulator for {len(topo.zones)} zones ({len(rooms)} rooms and "
        f"{len(corridors)} corridors with doorways), {len(topo.connections)} "
        f"doorways, {len(topo.stairs)} stair links, "
        f"{sum(len(c) for c in topo.cells.values())} corridor cells; "
        f"scenario mode {SCENARIO_MODE}, time scale x{TIME_SCALE}",
        flush=True,
    )

    sim_bus = SimBus().start()
    actuators = BuildSimActuators()
    doors = BuildSimDoors(topo)
    sim_bus.clear_all_truth(topo)
    model = new_model(topo)
    sim_bus.publish_scenario(model.scenario)

    last = time.monotonic()
    sim_time = 0.0   # simulated seconds since start, across scenarios
    tick = 0
    life = sim_bus.life
    life.running(f"{len(topo.zones)} zones")

    while not life.stopping:
        try:
            life.beat()
            if model.finished:
                model = new_model(topo, model.scenario["id"])
                sim_bus.publish_scenario(model.scenario)

            if actuators.read():
                life.ok()
            elif actuators.failures >= 3:
                life.degraded("cannot read actuator state from BuildSim "
                              "(keeping the last known states)")
            actuators.apply(model)
            doors.apply(model)

            now = time.monotonic()
            step = (now - last) * TIME_SCALE
            model.step(step)
            sim_time += step
            last = now

            sim_bus.publish_truth(model)
            sim_bus.publish_clock(model, sim_time)
            publish_buildsim(model)
            tick += 1
            if model.active or tick % 15 == 0:
                print(f"t={model.time:6.0f}s active zones: {len(model.active_zones)}", flush=True)
                log(model)

        except Exception as exc:  # noqa: BLE001
            print(f"fire simulation tick failed: {exc}", flush=True)

        life.sleep(INTERVAL)
    life.stop()


if __name__ == "__main__":
    main()
