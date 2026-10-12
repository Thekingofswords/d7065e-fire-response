"""Record the MQTT bus to CSV files (no database needed).

Writes RECORD_DIR/scenario_<id>.csv (dataset.py schema) and
scenarios.jsonl, the format train_model.py reads. With the database running,
db_writer.py records the same samples there (and export_dataset.py turns
them back into these files).
"""
import os

import datastore


def main():
    datastore.Recorder("csv-recorder", [
        datastore.CsvSink(os.environ.get("RECORD_DIR", "/recordings"))]).run()


if __name__ == "__main__":
    main()
