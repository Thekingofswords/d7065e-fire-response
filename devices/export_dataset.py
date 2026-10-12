"""Export recorded samples from the database as training CSV files.

    python export_dataset.py --out ../recordings/db [--since "2026-10-01"] [--scenario 1791234567]

Writes one scenario_<id>.csv per scenario (dataset.py schema) plus
scenarios.jsonl, ready for train_model.py.
"""
import argparse
import json
import os

import dataset


def main():
    import psycopg

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get(
        "DATABASE_URL", "postgresql://fire:fire@localhost:5433/fire"))
    parser.add_argument("--out", default="recordings/db")
    parser.add_argument("--since", default=None, help="only samples after this time")
    parser.add_argument("--scenario", type=int, action="append", help="only these scenarios")
    args = parser.parse_args()

    cols = [c for c in dataset.COLUMNS if c != "wall_time"]
    where, params = ["scenario_id IS NOT NULL"], []
    if args.since:
        where.append("time >= %s")
        params.append(args.since)
    if args.scenario:
        where.append("scenario_id = ANY(%s)")
        params.append(args.scenario)
    query = (f"SELECT extract(epoch FROM time), {', '.join(cols)} FROM samples "
             f"WHERE {' AND '.join(where)} ORDER BY scenario_id, time, level, room")

    os.makedirs(args.out, exist_ok=True)
    writer = dataset.ScenarioWriter(args.out)
    count = 0
    with psycopg.connect(args.database_url) as conn, conn.cursor(name="export") as cur:
        cur.itersize = 20000
        cur.execute(query, params)
        for record in cur:
            row = {"wall_time": f"{float(record[0]):.1f}"}
            for c, v in zip(cols, record[1:]):
                row[c] = "" if v is None else v
            writer.write([row])
            count += 1
    writer.close()
    with psycopg.connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT params FROM scenarios ORDER BY scenario_id")
        with open(os.path.join(args.out, "scenarios.jsonl"), "w", encoding="utf-8") as f:
            for (params_json,) in cur:
                f.write(json.dumps(params_json) + "\n")
    print(f"exported {count} samples to {args.out}")


if __name__ == "__main__":
    main()
