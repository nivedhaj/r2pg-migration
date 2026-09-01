#!/usr/bin/env python3
"""
Master Migration Orchestrator: RavenDB to PostgreSQL
1. Runs data migration scripts (extract from RavenDB, create tables dynamically, load data).
2. Applies post-migration SQL (views, performance indexes, and triggers).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
import psycopg2

MODULE_SCRIPTS = [
    ("personas", "personas_ravendb_to_postgres_migrate.py"),
    ("courses", "courses_ravendb_to_postgres_migrate.py"),
    ("staffs", "staffs_ravendb_to_postgres_migrate.py"),
    ("students", "students_ravendb_to_postgres_migrate.py"),
    ("fees", "fees_ravendb_to_postgres_migrate.py"),
    ("exams", "exams_ravendb_to_postgres_migrate.py"),
]

def load_env_file(env_path: Path):
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and os.getenv(k) is None:
                os.environ[k] = v

def run_script(script_path: Path, extra_args: list[str]) -> bool:
    print("\n" + "=" * 65)
    print(f"[*] Starting Migration: {script_path.name}")
    print("=" * 65)
    cmd = [sys.executable, str(script_path)] + extra_args
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[!] Error: {script_path.name} failed with exit code {result.returncode}")
        return False
    print(f"[+] Success: {script_path.name} completed successfully.")
    return True

def apply_post_migration_sql(scripts_dir: Path):
    print("\n" + "=" * 65)
    print("[*] Applying Post-Migration Views, Indexes & Triggers")
    print("=" * 65)
    
    # Try finding sql directory
    sql_dirs = [
        scripts_dir.parent / "student-fee-poc" / "sql",
        scripts_dir.parent / "sql",
        scripts_dir / "sql"
    ]
    sql_dir = next((d for d in sql_dirs if d.exists()), None)
    
    if not sql_dir:
        print("[!] Warning: sql directory not found for post-migration views. Skipping.")
        return

    pg_host = os.getenv("PG_HOST")
    pg_port = int(os.getenv("PG_PORT", "5432")) if os.getenv("PG_PORT") else 5432
    pg_db = os.getenv("PG_DB")
    pg_user = os.getenv("PG_USER")
    pg_password = os.getenv("PG_PASSWORD")

    sql_files = ["01_student_fee_view.sql", "02_trigger.sql"]
    
    try:
        conn = psycopg2.connect(
            host=pg_host,
            port=pg_port,
            dbname=pg_db,
            user=pg_user,
            password=pg_password
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            for sql_file in sql_files:
                file_path = sql_dir / sql_file
                if file_path.exists():
                    print(f"Applying SQL: {sql_file}...")
                    sql_content = file_path.read_text(encoding="utf-8")
                    cur.execute(sql_content)
                    print(f"[+] Applied {sql_file} successfully.")
                else:
                    print(f"[!] SQL file {sql_file} not found in {sql_dir}.")
        conn.close()
    except Exception as ex:
        print(f"[!] Warning during post-migration SQL execution: {ex}")

def main():
    scripts_dir = Path(__file__).parent.resolve()
    # Load central root .env
    root_env = scripts_dir.parent / ".env"
    if root_env.exists():
        load_env_file(root_env)

    parser = argparse.ArgumentParser(description="Master RavenDB -> PostgreSQL Migration Runner")
    parser.add_argument("--all", action="store_true", help="Run all migrations in order")
    parser.add_argument("--module", "-m", help="Comma-separated list of modules to migrate (e.g., student,fees)")
    parser.add_argument("--skip-post-sql", action="store_true", help="Skip applying post-migration views & triggers")
    
    args, unknown = parser.parse_known_args()

    selected_modules = []
    if args.all or not args.module:
        selected_modules = [m[0] for m in MODULE_SCRIPTS]
    else:
        raw_modules = [m.strip().lower() for m in args.module.split(",")]
        selected_modules = ["students" if m == "student" else m for m in raw_modules]

    print(f"[*] Queued migration modules: {', '.join(selected_modules)}")
    
    success_count = 0
    failure_count = 0

    for name, script_file in MODULE_SCRIPTS:
        if name in selected_modules:
            script_full_path = scripts_dir / script_file
            if not script_full_path.exists():
                print(f"[!] Warning: Script {script_file} not found. Skipping.")
                failure_count += 1
                continue
            
            ok = run_script(script_full_path, unknown)
            if ok:
                success_count += 1
            else:
                failure_count += 1

    print("\n" + "=" * 65)
    print(f"[=] MIGRATION SUMMARY: {success_count} succeeded, {failure_count} failed")
    print("=" * 65)

    if success_count > 0 and not args.skip_post_sql:
        apply_post_migration_sql(scripts_dir)

    if failure_count > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
