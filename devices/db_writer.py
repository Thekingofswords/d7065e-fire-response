"""Store everything on the MQTT bus in TimescaleDB / PostgreSQL.

    DATABASE_URL=postgresql://fire:fire@timescaledb:5432/fire python db_writer.py

Tables (created automatically, see datastore.SCHEMA):
  samples          one row per zone per RECORD_INTERVAL: readings, sprinkler,
                   simulator truth and labels (the training dataset schema)
  events           alarms, room state changes, sensor faults, commands applied
  commands         every actuator command that was sent
  actuator_states  every confirmed actuator state
  scenarios        the simulator's scenarios

Set RECORD_DIR as well to also write the CSV files. Export a training set
with export_dataset.py.
"""
import os

import datastore


def main():
    sinks = [datastore.PostgresSink(os.environ.get(
        "DATABASE_URL", "postgresql://fire:fire@timescaledb:5432/fire"))]
    if os.environ.get("RECORD_DIR"):
        sinks.append(datastore.CsvSink(os.environ["RECORD_DIR"]))
    datastore.Recorder("db-writer", sinks).run()


if __name__ == "__main__":
    main()
