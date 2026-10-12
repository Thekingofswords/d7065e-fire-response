"""Cold storage: copy finished days of history from the database to Parquet.

    python archive.py --out ../archive            # once
    python archive.py --out /archive --every 6     # as a service, every 6 hours

For every complete day (UTC) not yet archived it writes

    <out>/samples/date=YYYY-MM-DD/samples.parquet      raw samples
    <out>/events/date=YYYY-MM-DD/events.parquet        decisions, commands, faults
    <out>/commands/..., <out>/actuator_states/...

(Hive-style partitions, readable by pandas, DuckDB, Spark). Without pyarrow
it writes .csv.gz instead. Together with the database's retention policy
(RETENTION_DAYS, datastore.py) this is the hot/cold split: the database
keeps recent raw data for control and dashboards, the archive keeps
everything for training and long-term analysis.
"""
import argparse
import csv
import datetime
import gzip
import os
import time

import lifecycle
import mqtt_bus

TABLES = ("samples", "events", "commands", "actuator_states")


def write_table(path_base, columns, rows):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        data = {c: [r[i] for r in rows] for i, c in enumerate(columns)}
        for c, values in data.items():   # JSON columns as text
            if any(isinstance(v, (dict, list)) for v in values):
                import json
                data[c] = [json.dumps(v) if v is not None else None for v in values]
        pq.write_table(pa.table(data), path_base + ".parquet", compression="zstd")
        return path_base + ".parquet"
    except ImportError:
        with gzip.open(path_base + ".csv.gz", "wt", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)
        return path_base + ".csv.gz"


def archived(out, table, day):
    folder = os.path.join(out, table, f"date={day.isoformat()}")
    return os.path.isdir(folder) and any(n.startswith(table) for n in os.listdir(folder))


def archive_once(url, out, keep_days=0):
    import psycopg
    written = []
    with psycopg.connect(url) as conn:
        first = conn.execute("SELECT min(time) FROM samples").fetchone()[0]
        if first is None:
            return written
        today = datetime.datetime.now(datetime.timezone.utc).date()
        day = first.astimezone(datetime.timezone.utc).date()
        while day < today - datetime.timedelta(days=keep_days):
            start = datetime.datetime.combine(day, datetime.time(), datetime.timezone.utc)
            end = start + datetime.timedelta(days=1)
            for table in TABLES:
                if archived(out, table, day):
                    continue
                cur = conn.execute(f"SELECT * FROM {table} WHERE time >= %s AND time < %s "
                                   f"ORDER BY time", (start, end))
                rows = cur.fetchall()
                if not rows:
                    continue
                columns = [d.name for d in cur.description]
                folder = os.path.join(out, table, f"date={day.isoformat()}")
                os.makedirs(folder, exist_ok=True)
                path = write_table(os.path.join(folder, table), columns, rows)
                written.append((path, len(rows)))
                print(f"archived {len(rows)} {table} rows of {day} to {path}", flush=True)
            day += datetime.timedelta(days=1)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get(
        "DATABASE_URL", "postgresql://fire:fire@localhost:5433/fire"))
    parser.add_argument("--out", default=os.environ.get("ARCHIVE_DIR", "archive"))
    parser.add_argument("--every", type=float, default=0.0,
                        help="hours between runs (0: run once and exit)")
    parser.add_argument("--include-today", action="store_true",
                        help="also archive today so far (testing)")
    args = parser.parse_args()
    keep = -1 if args.include_today else 0
    if not args.every:
        archive_once(args.database_url, args.out, keep)
        return
    life = lifecycle.Lifecycle("archiver", status_topic=mqtt_bus.service_status_topic("archiver"))
    life.start()
    life.running(f"every {args.every} h to {args.out}")
    next_run = 0.0
    while not life.stopping:
        life.beat()
        if time.monotonic() >= next_run:
            try:
                archive_once(args.database_url, args.out, keep)
                life.ok()
            except Exception as exc:  # noqa: BLE001
                life.degraded(f"archive failed: {exc}")
            next_run = time.monotonic() + args.every * 3600
        life.sleep(10)
    life.stop()


if __name__ == "__main__":
    main()
