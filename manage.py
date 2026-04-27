#!/usr/bin/env python3
"""
Badge Scanner Management CLI

Manage script for the Snowflake Summit Badge Scanner pipeline.
Provides subcommands to control services, run scans, query results,
and verify system health.

Subcommands:
  start      Start the Ollama LLM service (systemctl).
  stop       Stop the Ollama LLM service.
  scan       Run the full 7-step badge scanning pipeline.
  list       Show recent scans from DEMO.DEMO.BADGE_SCANS.
  test       Pre-flight health checks (camera, sensor, Ollama, Snowflake).
  validate   Static code validation of badge_scanner.py.

Usage:
  python3 manage.py start
  python3 manage.py stop
  python3 manage.py scan
  python3 manage.py list              # last 10 scans
  python3 manage.py list --limit 25   # last 25 scans
  python3 manage.py test
  python3 manage.py validate

Prerequisites:
  - Raspberry Pi 5 with USB webcam, BMP280 on I2C bus 1
  - Ollama installed with moondream model pulled
  - Snowflake connection 'cortexcli1' configured in ~/.snowflake/connections.toml
  - Python packages: picamera2, pyzbar, Pillow, snowflake-connector-python,
    smbus2, requests
"""

import argparse
import ast
import json
import os
import struct
import subprocess
import sys

# ---------------------------------------------------------------------------
# Shared constants (must match badge_scanner.py)
# ---------------------------------------------------------------------------
SCANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "badge_scanner.py")
SNOWFLAKE_CONNECTION = "cortexcli1"
DATABASE = "DEMO"
SCHEMA = "DEMO"
WAREHOUSE = "INGEST"
STAGE = "BADGE_SCAN_STAGE"
TABLE = "BADGE_SCANS"
LLM_RESULTS_TABLE = "LOCAL_LLM_RESULTS"
BMP280_I2C_BUS = 1
BMP280_I2C_ADDR = 0x76
OLLAMA_URL = "http://localhost:11434"
LOCAL_LLM_MODEL = "moondream"


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

def cmd_start(args):
    """Start services and verify dependencies."""
    failed = False

    # 1. Ensure libzbar0 system library (required by pyzbar)
    print("Checking libzbar0...")
    r = subprocess.run(["dpkg", "-s", "libzbar0"], capture_output=True, text=True)
    if r.returncode != 0:
        print("  libzbar0 not found — installing...")
        r = subprocess.run(["sudo", "apt-get", "install", "-y", "libzbar0"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  FAILED: {r.stderr.strip()}")
            failed = True
        else:
            print("  libzbar0 installed.")
    else:
        print("  libzbar0 OK.")

    # 2. Ensure pyzbar Python package
    print("Checking pyzbar...")
    try:
        import pyzbar.pyzbar  # noqa: F401
        print("  pyzbar OK.")
    except ImportError:
        print("  pyzbar not found — installing...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages", "pyzbar"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"  FAILED: {r.stderr.strip()}")
            failed = True
        else:
            print("  pyzbar installed.")

    # 3. Start Ollama service
    print("Starting Ollama service...")
    r = subprocess.run(["systemctl", "is-active", "ollama"], capture_output=True, text=True)
    if r.stdout.strip() == "active":
        print("  Ollama is already running.")
    else:
        r = subprocess.run(["sudo", "systemctl", "start", "ollama"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  FAILED: {r.stderr.strip()}")
            failed = True
        else:
            print("  Ollama started.")

    return 1 if failed else 0


def cmd_stop(args):
    """Stop the Ollama service via systemctl."""
    print("Stopping Ollama service...")
    r = subprocess.run(["systemctl", "is-active", "ollama"], capture_output=True, text=True)
    if r.stdout.strip() != "active":
        print("  Ollama is not running.")
        return 0
    r = subprocess.run(["sudo", "systemctl", "stop", "ollama"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAILED: {r.stderr.strip()}")
        return 1
    print("  Ollama stopped.")
    return 0


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def cmd_scan(args):
    """Run the full badge scanning pipeline."""
    # Import inline to avoid heavy dependencies when running other commands
    sys.path.insert(0, os.path.dirname(SCANNER_PATH))
    import badge_scanner
    try:
        badge_scanner.main()
        return 0
    except Exception as e:
        print(f"\nScan failed: {e}")
        return 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(args):
    """Query and display recent scans from Snowflake."""
    try:
        import snowflake.connector
    except ImportError:
        print("ERROR: snowflake-connector-python is not installed.")
        return 1

    limit = args.limit
    try:
        conn = snowflake.connector.connect(
            connection_name=SNOWFLAKE_CONNECTION,
            database=DATABASE,
            schema=SCHEMA,
            warehouse=WAREHOUSE,
        )
        cur = conn.cursor()
        cur.execute(f"""
            SELECT ID, SCAN_TIMESTAMP, IMAGE_FILENAME, PARSED_NAME,
                   PARSED_COMPANY, PROCESSING_STATUS,
                   METADATA:sensor_readings:temperature_f::STRING AS TEMP_F,
                   METADATA:local_llm:model::STRING AS LLM_MODEL
            FROM {DATABASE}.{SCHEMA}.{TABLE}
            ORDER BY ID DESC
            LIMIT {int(limit)}
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    if not rows:
        print("No scans found.")
        return 0

    # Header
    print(f"\n{'ID':>6}  {'Timestamp':<20} {'Name':<22} {'Company':<20} {'Status':<10} {'Temp':>6} {'LLM':<12}")
    print("-" * 100)
    for row in rows:
        rid = row[0] or ""
        ts = str(row[1])[:19] if row[1] else ""
        name = (row[3] or "")[:21]
        company = (row[4] or "")[:19]
        status = (row[5] or "")[:9]
        temp = (row[6] or "")[:6]
        llm = (row[7] or "")[:11]
        print(f"{rid:>6}  {ts:<20} {name:<22} {company:<20} {status:<10} {temp:>6} {llm:<12}")
    print(f"\n{len(rows)} scan(s) shown.\n")
    return 0


# ---------------------------------------------------------------------------
# test  (pre-flight health checks)
# ---------------------------------------------------------------------------

def cmd_test(args):
    """Run pre-flight checks on all subsystems."""
    checks = []

    def check(name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        checks.append(passed)
        mark = "+" if passed else "!"
        line = f"  [{mark}] {name:<35} {status}"
        if detail:
            line += f"  ({detail})"
        print(line)

    print("\n=== Pre-flight Health Checks ===\n")

    # 1. Camera
    cam_ok = os.path.exists("/dev/video0")
    check("USB Camera /dev/video0", cam_ok)

    # 2. BMP280 I2C sensor
    sensor_ok = False
    sensor_detail = ""
    try:
        import smbus2
        bus = smbus2.SMBus(BMP280_I2C_BUS)
        chip_id = bus.read_byte_data(BMP280_I2C_ADDR, 0xD0)
        bus.close()
        sensor_ok = chip_id == 0x58
        sensor_detail = f"chip_id=0x{chip_id:02X}"
    except Exception as e:
        sensor_detail = str(e)
    check("BMP280 I2C sensor (bus 1, 0x76)", sensor_ok, sensor_detail)

    # 3. Ollama service
    ollama_active = False
    try:
        r = subprocess.run(["systemctl", "is-active", "ollama"], capture_output=True, text=True)
        ollama_active = r.stdout.strip() == "active"
    except Exception:
        pass
    check("Ollama service running", ollama_active)

    # 4. Ollama API reachable
    api_ok = False
    api_detail = ""
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        api_ok = True
        api_detail = f"{len(models)} model(s)"
    except Exception as e:
        api_detail = str(e)
    check("Ollama API reachable", api_ok, api_detail)

    # 5. moondream model available
    model_ok = False
    if api_ok:
        model_ok = any(LOCAL_LLM_MODEL in m for m in models)
    check(f"Model '{LOCAL_LLM_MODEL}' loaded", model_ok)

    # 6. Snowflake connection
    sf_ok = False
    sf_detail = ""
    try:
        import snowflake.connector
        conn = snowflake.connector.connect(
            connection_name=SNOWFLAKE_CONNECTION,
            database=DATABASE,
            schema=SCHEMA,
            warehouse=WAREHOUSE,
        )
        cur = conn.cursor()
        cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE()")
        row = cur.fetchone()
        sf_ok = True
        sf_detail = f"user={row[0]}, role={row[1]}"
        cur.close()
    except Exception as e:
        sf_detail = str(e)
    check("Snowflake connection", sf_ok, sf_detail)

    # 7. Stage exists
    stage_ok = False
    if sf_ok:
        try:
            cur = conn.cursor()
            cur.execute(f"DESCRIBE STAGE {DATABASE}.{SCHEMA}.{STAGE}")
            stage_ok = True
            cur.close()
        except Exception as e:
            sf_detail = str(e)
    check(f"Stage {STAGE}", stage_ok)

    # 8. Table exists
    table_ok = False
    table_detail = ""
    if sf_ok:
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {DATABASE}.{SCHEMA}.{TABLE}")
            count = cur.fetchone()[0]
            table_ok = True
            table_detail = f"{count} row(s)"
            cur.close()
        except Exception as e:
            table_detail = str(e)
    check(f"Table {TABLE}", table_ok, table_detail)

    # 8b. LLM results table exists
    llm_table_ok = False
    llm_table_detail = ""
    if sf_ok:
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {DATABASE}.{SCHEMA}.{LLM_RESULTS_TABLE}")
            count = cur.fetchone()[0]
            llm_table_ok = True
            llm_table_detail = f"{count} row(s)"
            cur.close()
        except Exception as e:
            llm_table_detail = str(e)
    check(f"Table {LLM_RESULTS_TABLE}", llm_table_ok, llm_table_detail)

    # 9. badge_scanner.py exists
    scanner_ok = os.path.isfile(SCANNER_PATH)
    check("badge_scanner.py exists", scanner_ok, SCANNER_PATH)

    # 10. Python dependencies
    deps_ok = True
    missing = []
    for mod in ["picamera2", "pyzbar", "PIL", "snowflake.connector", "smbus2", "requests"]:
        try:
            __import__(mod)
        except ImportError:
            deps_ok = False
            missing.append(mod)
    check("Python dependencies", deps_ok, ", ".join(missing) if missing else "all present")

    if sf_ok:
        conn.close()

    passed = sum(checks)
    total = len(checks)
    print(f"\n  Result: {passed}/{total} checks passed.\n")
    return 0 if all(checks) else 1


# ---------------------------------------------------------------------------
# validate  (static code analysis)
# ---------------------------------------------------------------------------

def cmd_validate(args):
    """Run static validation on badge_scanner.py."""
    print(f"\n=== Validating {SCANNER_PATH} ===\n")
    errors = []

    # 1. Syntax check
    print("  [1] Syntax check...")
    try:
        with open(SCANNER_PATH) as f:
            source = f.read()
        compile(source, SCANNER_PATH, "exec")
        print("      PASS")
    except SyntaxError as e:
        print(f"      FAIL: {e}")
        errors.append("syntax")

    # 2. AST analysis
    print("  [2] AST analysis...")
    try:
        tree = ast.parse(source)
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        bare_excepts = sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.ExceptHandler) and n.type is None
        )
        print(f"      Functions: {len(funcs)} ({', '.join(funcs)})")
        print(f"      Bare except clauses: {bare_excepts}")
        if bare_excepts > 0:
            errors.append("bare_except")
    except Exception as e:
        print(f"      FAIL: {e}")
        errors.append("ast")

    # 3. Pipeline coverage -- verify main() calls all pipeline functions
    print("  [3] Pipeline coverage...")
    pipeline_funcs = [
        "capture_image", "read_bmp280_sensor", "scan_qr_codes",
        "get_snowflake_connection", "upload_to_stage", "analyze_with_ai",
        "run_llm_and_store", "store_metadata", "display_results",
    ]
    try:
        main_node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = set()
        for n in ast.walk(main_node):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    calls.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    calls.add(n.func.attr)
                # Also catch functions passed as keyword args (e.g. target=run_llm_and_store)
                for kw in n.keywords:
                    if isinstance(kw.value, ast.Name):
                        calls.add(kw.value.id)
        missing = [f for f in pipeline_funcs if f not in calls]
        if missing:
            print(f"      FAIL: missing calls: {', '.join(missing)}")
            errors.append("pipeline_coverage")
        else:
            print(f"      PASS ({len(pipeline_funcs)}/{len(pipeline_funcs)} pipeline functions called)")
    except StopIteration:
        print("      FAIL: main() not found")
        errors.append("no_main")

    # 4. INSERT column alignment with table schema
    print("  [4] Schema alignment...")
    insert_cols = [
        "SCAN_TIMESTAMP", "IMAGE_FILENAME", "IMAGE_STAGE_PATH",
        "QR_CODE_DATA", "EXTRACTED_TEXT",
        "PARSED_NAME", "PARSED_TITLE", "PARSED_COMPANY",
        "PARSED_EMAIL", "PARSED_PHONE",
        "CONFIDENCE_SCORE", "PROCESSING_STATUS",
        "CREATED_BY", "METADATA", "NOTES", "CONFERENCE_NAME",
    ]
    table_cols = [
        "ID", "SCAN_TIMESTAMP", "IMAGE_FILENAME", "IMAGE_STAGE_PATH",
        "QR_CODE_DATA", "EXTRACTED_TEXT", "PARSED_NAME", "PARSED_TITLE",
        "PARSED_COMPANY", "PARSED_EMAIL", "PARSED_PHONE",
        "CONFIDENCE_SCORE", "PROCESSING_STATUS", "CREATED_BY",
        "METADATA", "NOTES", "SECOND_EMAIL", "SECOND_PHONE",
        "PERSON_PHOTO_PATH", "CONFERENCE_NAME", "BADGE_IMAGE_URL",
    ]
    bad_cols = [c for c in insert_cols if c not in table_cols]
    if bad_cols:
        print(f"      FAIL: columns not in table: {', '.join(bad_cols)}")
        errors.append("schema")
    else:
        unused = [c for c in table_cols if c not in insert_cols and c != "ID"]
        print(f"      PASS ({len(insert_cols)} INSERT cols, {len(unused)} unused nullable cols)")

    # 5. Bind parameter count
    print("  [5] Bind parameter count...")
    # SCAN_TIMESTAMP uses CURRENT_TIMESTAMP(), METADATA uses PARSE_JSON(%s)
    # So 16 columns - 1 (SCAN_TIMESTAMP) = 15 bind params
    expected_params = 15
    # Count %s in the INSERT SQL pattern in source
    # Find the INSERT block
    idx = source.find("INSERT INTO")
    if idx >= 0:
        block = source[idx:source.find("cur.execute(sql", idx)]
        param_count = block.count("%s")
        if param_count == expected_params:
            print(f"      PASS ({param_count} bind parameters)")
        else:
            print(f"      FAIL: expected {expected_params}, found {param_count}")
            errors.append("bind_params")
    else:
        print("      SKIP: INSERT statement not found in source")

    # Summary
    if errors:
        print(f"\n  FAILED: {len(errors)} issue(s): {', '.join(errors)}\n")
        return 1
    else:
        print(f"\n  ALL CHECKS PASSED.\n")
        return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Badge Scanner Management CLI — start/stop services, run scans, query results, verify health.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="Start the Ollama LLM service")
    sub.add_parser("stop", help="Stop the Ollama LLM service")
    sub.add_parser("scan", help="Run the full badge scanning pipeline")

    p_list = sub.add_parser("list", help="Show recent scans from Snowflake")
    p_list.add_argument("--limit", type=int, default=10, help="Number of rows (default 10)")

    sub.add_parser("test", help="Pre-flight health checks")
    sub.add_parser("validate", help="Static code validation of badge_scanner.py")

    args = parser.parse_args()

    dispatch = {
        "start": cmd_start,
        "stop": cmd_stop,
        "scan": cmd_scan,
        "list": cmd_list,
        "test": cmd_test,
        "validate": cmd_validate,
    }

    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
