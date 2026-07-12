"""Release stale DB locks before a run.

A force-killed run can leave a backend stuck in `idle in transaction` holding
locks on the output tables, which then blocks the next run's ALTER TABLE /
CREATE INDEX (observed: a killed db_load INSERT blocking db_prepare's CREATE
INDEX on ref_retail_com). This terminates ONLY sessions that are
`idle in transaction` and have been so longer than DB_UNLOCK_IDLE_SECONDS
(default 60), excluding this connection. It never touches `active` (running)
queries or fresh sessions. Set DB_UNLOCK_DRY_RUN=1 to only report.
"""
import os

from .step00_config import db_config


def main():
    idle_seconds = max(5, int(os.getenv("DB_UNLOCK_IDLE_SECONDS", "60")))
    dry_run = os.getenv("DB_UNLOCK_DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "y"}
    config = db_config()
    if not config:
        print("[db_unlock] DB_CONFIG missing; skipping")
        return
    try:
        import psycopg2
    except ImportError:
        print("[db_unlock] psycopg2 unavailable; skipping")
        return
    try:
        conn = psycopg2.connect(
            host=config.get("host"),
            port=int(config.get("port") or 5432),
            user=config.get("user"),
            password=config.get("password"),
            dbname=config.get("database"),
            connect_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - unlock is best-effort, never block the run
        print(f"[db_unlock] connect failed ({type(exc).__name__}: {exc}); skipping")
        return

    terminated = 0
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pid, state_change,
                       left(regexp_replace(coalesce(query, ''), '\\s+', ' ', 'g'), 100)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state = 'idle in transaction'
                  AND state_change < now() - make_interval(secs => %s)
                ORDER BY state_change
                """,
                (idle_seconds,),
            )
            rows = cur.fetchall()
            if not rows:
                print(f"[db_unlock] no idle-in-transaction sessions older than {idle_seconds}s")
            for pid, since, query in rows:
                if dry_run:
                    print(f"[db_unlock] (dry-run) would terminate pid={pid} idle_since={since} query={query!r}")
                    continue
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
                ok = bool(cur.fetchone()[0])
                terminated += 1 if ok else 0
                print(f"[db_unlock] terminated pid={pid} ok={ok} idle_since={since} query={query!r}")
    finally:
        conn.close()
    print(f"[db_unlock] done; terminated={terminated} dry_run={dry_run} threshold={idle_seconds}s")


if __name__ == "__main__":
    main()
