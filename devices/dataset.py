"""Dataset schema shared by recorder.py, generate_dataset.py and train_model.py.

One CSV row per room per tick. Each scenario is written to its own file,
scenario_<id>.csv, so a whole fire can be held out for testing.

Columns
  scenario_id, scenario_time  scenario and simulated seconds since it began
  wall_time                   UNIX time of a live recording (empty offline)
  level, room
  temperature, smoke, co, co2 sensor readings (what an agent sees)
  sprinkler                   1 if the room's sprinkler was on
  true_*                      physical truth from the simulator
  fire_phase                  0 idle, 1 smouldering, 2 growing,
                              3 fully developed, 4 decaying,
                              5 extinguished, 6 burnt out
  fire                        1 if the room is burning (phase 1-4): the
                              training target
  fire_origin                 1 if the fire started in this room
  nuisance_source             0 none, 1 cooking, 2 steam, 3 dust,
                              4 heater, 5 smoking
  sensor_fault                0 none, 1 smoke drift, 2 temperature stuck,
                              3 smoke spikes, 4 CO dead, 5 sensor offline
  stale_signals               bitmask of signals whose sensor stopped
                              reporting (1 temperature, 2 smoke, 4 co,
                              8 co2), as seen by the agent
"""
import csv
import glob
import os

import common
import scenarios

TRUTH_LAYERS = [
    common.LAYER_HRR,
    common.LAYER_GAS_TEMP,
    common.LAYER_OBSCURATION,
    common.LAYER_VISIBILITY,
    common.LAYER_CO,
    common.LAYER_CO2,
    common.LAYER_O2,
    common.LAYER_SMOKE_LAYER_HEIGHT,
]

COLUMNS = (
    ["scenario_id", "scenario_time", "wall_time", "level", "room",
     "temperature", "smoke", "co", "co2", "sprinkler"]
    + [f"true_{name}" for name in TRUTH_LAYERS]
    + ["fire_phase", "fire", "fire_origin", "nuisance_source", "sensor_fault",
       "stale_signals"]
)

TEXT_COLUMNS = {"level", "room", "wall_time"}


def make_row(room, scenario_id, scenario_time, readings, sprinkler, truth,
             wall_time="", stale_mask=0):
    """Build one CSV row (dict) from readings, truth and label values."""
    level, name = common.split_room(room)
    phase = int(truth.get(common.LAYER_FIRE_PHASE, 0))
    row = {
        "scenario_id": int(scenario_id),
        "scenario_time": round(scenario_time, 1),
        "wall_time": wall_time,
        "level": level,
        "room": name,
        "temperature": round(readings["temperature"], 2),
        "smoke": round(readings["smoke"], 3),
        "co": round(readings["co"], 1),
        "co2": round(readings["co2"], 0),
        "sprinkler": int(bool(sprinkler)),
        "fire_phase": phase,
        "fire": int(phase in scenarios.BURNING_PHASE_CODES),
        "fire_origin": int(truth.get(common.LAYER_FIRE_ORIGIN, 0)),
        "nuisance_source": int(truth.get(common.LAYER_NUISANCE, 0)),
        "sensor_fault": int(truth.get(common.LAYER_SENSOR_FAULT, 0)),
        "stale_signals": int(stale_mask),
    }
    for layer in TRUTH_LAYERS:
        value = truth.get(layer)
        row[f"true_{layer}"] = "" if value is None else round(value, 3)
    return row


class ScenarioWriter:
    """Appends rows to scenario_<id>.csv files in a directory."""

    def __init__(self, directory, prefix="scenario"):
        self.directory = directory
        self.prefix = prefix
        os.makedirs(directory, exist_ok=True)
        self.handles = {}

    def path(self, scenario_id):
        return os.path.join(self.directory, f"{self.prefix}_{int(scenario_id)}.csv")

    def write(self, rows):
        for row in rows:
            sid = row["scenario_id"]
            if sid not in self.handles:
                # Keep at most a couple of files open (scenarios are sequential).
                for old in list(self.handles):
                    self.handles.pop(old)[0].close()
                path = self.path(sid)
                new = not os.path.exists(path) or os.path.getsize(path) == 0
                handle = open(path, "a", newline="", encoding="utf-8")
                writer = csv.DictWriter(handle, fieldnames=COLUMNS)
                if new:
                    writer.writeheader()
                self.handles[sid] = (handle, writer)
            self.handles[sid][1].writerow(row)

    def flush(self):
        for handle, _ in self.handles.values():
            handle.flush()

    def close(self):
        for handle, _ in self.handles.values():
            handle.close()
        self.handles = {}


def write_scenario_file(directory, scenario_id, rows, prefix="scenario"):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{prefix}_{int(scenario_id)}.csv")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _convert(column, value):
    if column in TEXT_COLUMNS:
        return value
    if value == "":
        return float("nan")
    return float(value)


def load(paths):
    """Load rows from CSV files and/or directories -> list of dicts."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += sorted(glob.glob(os.path.join(path, "*.csv")))
        else:
            files += sorted(glob.glob(path))
    rows = []
    for file in files:
        with open(file, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                row.setdefault("stale_signals", "0")  # older recordings
                rows.append({k: _convert(k, v) for k, v in row.items()})
    return rows
