#!/usr/bin/env python3
"""
Snowflake Summit Badge Scanner

End-to-end CLI pipeline that captures a conference badge image, reads
environmental sensor data, performs cloud and edge AI analysis, and
persists everything to Snowflake.

Pipeline steps:
  1. Capture  – USB webcam image via picamera2 (1920x1080 MJPEG).
  2. Sensor   – BMP280 temperature / pressure / altitude via I2C (smbus2).
  3. QR Scan  – Decode barcodes and QR codes with pyzbar.
  3b. Upload  – PUT image to Snowflake internal stage (SNOWFLAKE_SSE).
  4. Cloud AI – Cortex COMPLETE (pixtral-large multimodal) extracts badge text.
  5. Edge AI  – Ollama moondream local vision LLM analyses the badge image.
  6. Store    – INSERT metadata (VARIANT JSON) into DEMO.DEMO.BADGE_SCANS.
  7. Display  – Print formatted results to the terminal.

Hardware requirements:
  - Raspberry Pi 5 (tested) with USB webcam at /dev/video0
  - Pimoroni BMP280 on I2C bus 1, address 0x76
  - Ollama running locally (http://localhost:11434) with moondream pulled

Python dependencies:
  picamera2, pyzbar (+ libzbar0), Pillow, snowflake-connector-python,
  smbus2, requests

Snowflake objects (DEMO.DEMO):
  - Stage:  BADGE_SCAN_STAGE  (internal, SNOWFLAKE_SSE, directory enabled)
  - Table:  BADGE_SCANS       (21 columns, ID IDENTITY primary key)

Configuration is at the top of this file (connection name, database,
stage, model names, I2C addresses, Ollama URL).

Usage:
  python3 badge_scanner.py
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime

try:
    from picamera2 import Picamera2
except ImportError:
    sys.exit("Error: picamera2 is required. Install with: apt install python3-picamera2")

try:
    from pyzbar import pyzbar
except ImportError:
    sys.exit("Error: pyzbar is required. Install with: pip install pyzbar")

try:
    from PIL import Image
except ImportError:
    sys.exit("Error: Pillow is required. Install with: pip install pillow")

try:
    import snowflake.connector
except ImportError:
    sys.exit("Error: snowflake-connector-python is required. Install with: pip install snowflake-connector-python")

import struct
import smbus2
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SNOWFLAKE_CONNECTION = "cortexcli1"
DATABASE = "DEMO"
SCHEMA = "DEMO"
WAREHOUSE = "INGEST"
STAGE = "BADGE_SCAN_STAGE"
TABLE = "BADGE_SCANS"
AI_MODEL = "pixtral-large"
CAMERA_INDEX = 0

# BMP280 sensor
BMP280_I2C_BUS = 1
BMP280_I2C_ADDR = 0x76

# Local Ollama LLM
OLLAMA_URL = "http://localhost:11434"
LOCAL_LLM_MODEL = "moondream"
LOCAL_LLM_MODELS = ["moondream", "gemma4:e2b"]  # run both async
LOCAL_LLM_WAIT = 300  # seconds; wait for async LLM threads to finish
LLM_RESULTS_TABLE = "LOCAL_LLM_RESULTS"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_section(title):
    width = 60
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def print_field(label, value, indent=2):
    prefix = " " * indent
    if value:
        print(f"{prefix}{label:20s}: {value}")
    else:
        print(f"{prefix}{label:20s}: (not detected)")


# ---------------------------------------------------------------------------
# BMP280 Sensor Reading (smbus2 direct I2C)
# ---------------------------------------------------------------------------

def read_bmp280_sensor():
    """Read temperature and pressure from BMP280 via I2C.

    Uses the BMP280 datasheet compensation formulas to convert raw ADC
    readings into calibrated temperature (C) and pressure (hPa).
    Altitude is estimated from pressure using the barometric formula.
    """
    print_section("STEP 2: Reading BMP280 Sensor")

    try:
        bus = smbus2.SMBus(BMP280_I2C_BUS)

        # Verify chip ID
        chip_id = bus.read_byte_data(BMP280_I2C_ADDR, 0xD0)
        if chip_id != 0x58:
            print(f"  WARNING: Unexpected chip ID 0x{chip_id:02X} (expected 0x58)")

        # Read calibration data (registers 0x88-0x9F, 26 bytes)
        cal = bus.read_i2c_block_data(BMP280_I2C_ADDR, 0x88, 26)

        # Unpack calibration coefficients (little-endian)
        dig_T1 = struct.unpack_from('<H', bytes(cal), 0)[0]
        dig_T2 = struct.unpack_from('<h', bytes(cal), 2)[0]
        dig_T3 = struct.unpack_from('<h', bytes(cal), 4)[0]
        dig_P1 = struct.unpack_from('<H', bytes(cal), 6)[0]
        dig_P2 = struct.unpack_from('<h', bytes(cal), 8)[0]
        dig_P3 = struct.unpack_from('<h', bytes(cal), 10)[0]
        dig_P4 = struct.unpack_from('<h', bytes(cal), 12)[0]
        dig_P5 = struct.unpack_from('<h', bytes(cal), 14)[0]
        dig_P6 = struct.unpack_from('<h', bytes(cal), 16)[0]
        dig_P7 = struct.unpack_from('<h', bytes(cal), 18)[0]
        dig_P8 = struct.unpack_from('<h', bytes(cal), 20)[0]
        dig_P9 = struct.unpack_from('<h', bytes(cal), 22)[0]

        # Trigger a forced measurement: osrs_t=x2, osrs_p=x16, mode=forced
        #   ctrl_meas register 0xF4: osrs_t[7:5]=010, osrs_p[4:2]=101, mode[1:0]=01
        bus.write_byte_data(BMP280_I2C_ADDR, 0xF4, 0x55)

        # Wait for measurement to complete (~40ms for these oversampling settings)
        time.sleep(0.05)

        # Read raw data: pressure (0xF7-0xF9), temperature (0xFA-0xFC)
        raw = bus.read_i2c_block_data(BMP280_I2C_ADDR, 0xF7, 6)
        bus.close()

        adc_P = ((raw[0] << 16) | (raw[1] << 8) | raw[2]) >> 4
        adc_T = ((raw[3] << 16) | (raw[4] << 8) | raw[5]) >> 4

        # Temperature compensation (BMP280 datasheet section 4.2.3)
        var1 = ((adc_T / 16384.0) - (dig_T1 / 1024.0)) * dig_T2
        var2 = (((adc_T / 131072.0) - (dig_T1 / 8192.0)) ** 2) * dig_T3
        t_fine = var1 + var2
        temperature_c = t_fine / 5120.0

        # Pressure compensation (BMP280 datasheet section 4.2.3)
        var1 = (t_fine / 2.0) - 64000.0
        var2 = var1 * var1 * dig_P6 / 32768.0
        var2 = var2 + var1 * dig_P5 * 2.0
        var2 = (var2 / 4.0) + (dig_P4 * 65536.0)
        var1 = (dig_P3 * var1 * var1 / 524288.0 + dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * dig_P1
        if var1 == 0:
            pressure_hpa = 0
        else:
            p = 1048576.0 - adc_P
            p = (p - (var2 / 4096.0)) * 6250.0 / var1
            var1 = dig_P9 * p * p / 2147483648.0
            var2 = p * dig_P8 / 32768.0
            pressure_hpa = (p + (var1 + var2 + dig_P7) / 16.0) / 100.0

        # Estimate altitude from pressure using barometric formula
        # Standard sea-level pressure = 1013.25 hPa
        if pressure_hpa > 0:
            altitude_m = 44330.0 * (1.0 - (pressure_hpa / 1013.25) ** 0.1903)
            altitude_ft = altitude_m * 3.28084
        else:
            altitude_ft = 0.0

        temperature_f = temperature_c * 9.0 / 5.0 + 32.0

        print(f"  Temperature : {temperature_c:.1f} C / {temperature_f:.1f} F")
        print(f"  Pressure    : {pressure_hpa:.1f} hPa")
        print(f"  Est Altitude: {altitude_ft:.0f} ft")

        return {
            "temperature_c": round(temperature_c, 2),
            "temperature_f": round(temperature_f, 2),
            "pressure_hpa": round(pressure_hpa, 2),
            "altitude_ft": round(altitude_ft, 1),
        }

    except Exception as e:
        print(f"  ERROR reading BMP280 sensor: {e}")
        return {
            "temperature_c": None,
            "temperature_f": None,
            "pressure_hpa": None,
            "altitude_ft": None,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Step 1: Capture image from USB webcam
# ---------------------------------------------------------------------------

def capture_image(output_dir):
    """Capture a JPEG image from the USB webcam using picamera2."""
    print_section("STEP 1: Capturing Image from Webcam")

    filename = f"badge_{uuid.uuid4()}.jpg"
    filepath = os.path.join(output_dir, filename)

    cam = Picamera2(CAMERA_INDEX)
    config = cam.create_still_configuration(main={"format": "RGB888"})
    cam.configure(config)
    cam.start()
    # Let the camera auto-exposure settle
    time.sleep(2)
    cam.capture_file(filepath)
    cam.stop()
    cam.close()

    size_kb = os.path.getsize(filepath) / 1024
    print(f"  Captured image: {filename}")
    print(f"  Size: {size_kb:.1f} KB")
    print(f"  Local path: {filepath}")
    return filepath, filename


# ---------------------------------------------------------------------------
# Step 3: Scan QR codes / barcodes
# ---------------------------------------------------------------------------

def scan_qr_codes(filepath):
    """Scan all QR codes and barcodes in the captured image."""
    print_section("STEP 3: Scanning QR Codes & Barcodes")

    image = Image.open(filepath)
    decoded_objects = pyzbar.decode(image)

    results = []
    if not decoded_objects:
        print("  No QR codes or barcodes detected.")
    else:
        print(f"  Found {len(decoded_objects)} code(s):")
        for i, obj in enumerate(decoded_objects, 1):
            code_type = obj.type
            data = obj.data.decode("utf-8", errors="replace")
            results.append({"type": code_type, "data": data})
            print(f"    [{i}] Type: {code_type}")
            print(f"        Data: {data}")

    return results


# ---------------------------------------------------------------------------
# Step 3: Connect to Snowflake and upload to stage
# ---------------------------------------------------------------------------

def get_snowflake_connection():
    """Create a Snowflake connection using the connections.toml config."""
    conn = snowflake.connector.connect(
        connection_name=SNOWFLAKE_CONNECTION,
        database=DATABASE,
        schema=SCHEMA,
        warehouse=WAREHOUSE,
    )
    return conn


def upload_to_stage(conn, filepath, filename):
    """Upload the image file to the Snowflake internal stage."""
    print_section("STEP 3b: Uploading Image to Snowflake Stage")

    stage_path = f"@{DATABASE}.{SCHEMA}.{STAGE}"
    cur = conn.cursor()
    try:
        put_sql = f"PUT 'file://{filepath}' '{stage_path}/' AUTO_COMPRESS=FALSE OVERWRITE=TRUE"
        cur.execute(put_sql)
        result = cur.fetchone()
        status = result[6] if result and len(result) > 6 else "UNKNOWN"
        print(f"  File: {filename}")
        print(f"  Stage: {stage_path}")
        print(f"  Status: {status}")

        # Refresh directory table so AI can access the file
        cur.execute(f"ALTER STAGE {DATABASE}.{SCHEMA}.{STAGE} REFRESH")
        print("  Directory refreshed.")
    finally:
        cur.close()

    return f"{stage_path}/{filename}"


# ---------------------------------------------------------------------------
# Step 4: Run Cortex AI analysis on the image
# ---------------------------------------------------------------------------

def analyze_with_ai(conn, filename):
    """Use Snowflake Cortex COMPLETE (multimodal) to analyze the badge image."""
    print_section("STEP 4: Running Cortex AI Analysis")

    prompt = (
        "Analyze this Snowflake Summit conference badge image carefully. "
        "Extract ALL visible text and information including: "
        "full name, job title, company/organization, email address, phone number, "
        "conference name, badge type, any QR code visible text, and any other details. "
        "Return your response as valid JSON with these keys: "
        "name, title, company, email, phone, conference, badge_type, other_text, summary. "
        "If a field is not visible, set it to null. Only return JSON, no other text."
    )

    sql = f"""
        SELECT SNOWFLAKE.CORTEX.COMPLETE(
            '{AI_MODEL}',
            '{prompt.replace("'", "''")}',
            TO_FILE('@{DATABASE}.{SCHEMA}.{STAGE}', '{filename}')
        ) AS result
    """

    cur = conn.cursor()
    try:
        print(f"  Model: {AI_MODEL}")
        print(f"  Analyzing: {filename}")
        print("  Waiting for AI response...")
        cur.execute(sql)
        row = cur.fetchone()
        raw_result = row[0] if row else None
    finally:
        cur.close()

    if not raw_result:
        print("  WARNING: No response from AI model.")
        return None, None

    print(f"  Response received ({len(raw_result)} chars)")

    # Try to parse JSON from the response
    parsed = parse_ai_response(raw_result)
    return raw_result, parsed


def parse_ai_response(raw):
    """Attempt to extract JSON from the AI response text."""
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (``` markers)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Local LLM Analysis via Ollama (moondream)
# ---------------------------------------------------------------------------

def analyze_with_local_llm(parsed_ai, sensor_data, qr_results,
                           image_path=None, model=None):
    """Run local Ollama LLM inference for a given model.

    Both models receive the base64-encoded badge image via the ``images``
    field in ``/api/chat``:
    - **moondream** (vision): Not a thinking model.
    - **gemma4:e2b** (vision + thinking): response may contain ``thinking``
      and ``content`` fields.
    """
    model = model or LOCAL_LLM_MODEL
    print_section(f"Local LLM Analysis ({model} via Ollama)")

    # Build supplementary text context
    parts = []

    if parsed_ai:
        parts.append("Badge AI analysis results:")
        for k, v in parsed_ai.items():
            if v is not None:
                parts.append(f"  {k}: {v}")

    if qr_results:
        parts.append("QR codes found:")
        for qr in qr_results:
            parts.append(f"  [{qr['type']}] {qr['data']}")

    if sensor_data and sensor_data.get("temperature_c") is not None:
        parts.append("Environmental sensor readings:")
        parts.append(f"  Temperature: {sensor_data['temperature_c']} C / {sensor_data['temperature_f']} F")
        parts.append(f"  Pressure: {sensor_data['pressure_hpa']} hPa")
        parts.append(f"  Est. Altitude: {sensor_data['altitude_ft']} ft")

    context = "\n".join(parts) if parts else ""

    prompt = (
        "You are an assistant at the Snowflake Summit conference. "
        "Describe what you see on this conference badge image. "
    )
    if context:
        prompt += f"Additional extracted data:\n{context}\n\n"
    prompt += (
        "Provide a brief, friendly 2-3 sentence summary of this attendee and "
        "the current environmental conditions. If data is missing, note that."
    )

    # Base64-encode the badge image for all models
    images_b64 = []
    if image_path and os.path.isfile(image_path):
        with open(image_path, "rb") as f:
            images_b64.append(base64.b64encode(f.read()).decode("ascii"))
        print(f"  Image : {image_path} (base64, {len(images_b64[0]) // 1024} KB)")

    try:
        print(f"  Model : {model}")
        print(f"  Server: {OLLAMA_URL}")
        print("  Waiting for local LLM response...")

        # Pre-load model if not already in memory (avoids 500 errors on Pi)
        try:
            warmup = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "options": {"num_predict": 1},
                    "keep_alive": "60m",
                },
                timeout=(10, 300),
            )
            warmup.raise_for_status()
            print("  Model loaded.")
        except Exception:
            print("  WARNING: Model warmup failed, attempting inference anyway...")

        # Build message with image if available
        user_message = {"role": "user", "content": prompt}
        if images_b64:
            user_message["images"] = images_b64

        start_t = time.time()
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [user_message],
                "stream": True,
                "options": {"num_predict": 512},
                "keep_alive": "60m",
            },
            timeout=(30, 600),
            stream=True,
        )
        resp.raise_for_status()

        # Accumulate streamed response tokens
        # gemma4:e2b is a thinking model -- it may return thinking + content
        response_text = ""
        thinking_text = ""
        eval_count = 0
        buf = b""
        for data in resp.iter_content(chunk_size=4096):
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    chunk = json.loads(line)
                    msg = chunk.get("message", {})
                    # Thinking models put reasoning in "thinking" field
                    if msg.get("thinking"):
                        thinking_text += msg["thinking"]
                    response_text += msg.get("content", "")
                    if chunk.get("done"):
                        eval_count = chunk.get("eval_count", 0)

        elapsed = time.time() - start_t
        final_text = response_text.strip()

        print(f"  Response received ({len(final_text)} chars, {eval_count} tokens, {elapsed:.1f}s)")
        if thinking_text:
            print(f"  (Thinking: {len(thinking_text)} chars)")
        print(f"  ---")
        for line in final_text.split("\n"):
            print(f"  {line}")

        result = {
            "response": final_text,
            "model": model,
            "tokens": eval_count,
            "duration_s": round(elapsed, 1),
        }
        if thinking_text:
            result["thinking"] = thinking_text.strip()
        return result

    except requests.exceptions.ConnectionError:
        print("  ERROR: Cannot connect to Ollama. Is it running?")
        return {"response": None, "error": "connection_failed"}
    except requests.exceptions.Timeout:
        print("  ERROR: Ollama request timed out.")
        return {"response": None, "error": "timeout"}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"response": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Step 6: Store metadata in BADGE_SCANS table
# ---------------------------------------------------------------------------

def store_metadata(conn, filename, stage_path, qr_results, raw_ai, parsed_ai,
                    sensor_data=None):
    """Insert a row into the BADGE_SCANS table with all extracted data."""
    print_section("STEP 6: Storing Metadata in Snowflake")

    qr_data = "; ".join(r["data"] for r in qr_results) if qr_results else None

    # Extract fields from parsed AI response
    name = None
    title = None
    company = None
    email = None
    phone = None
    conference = None
    summary = None

    if parsed_ai:
        name = parsed_ai.get("name")
        title = parsed_ai.get("title")
        company = parsed_ai.get("company")
        email = parsed_ai.get("email")
        phone = parsed_ai.get("phone")
        conference = parsed_ai.get("conference")
        summary = parsed_ai.get("summary")

    # Build notes from AI summary
    notes = f"[Cortex AI] {summary}" if summary else None

    metadata = json.dumps({
        "qr_codes": qr_results,
        "ai_model": AI_MODEL,
        "ai_raw_response": raw_ai,
        "ai_parsed": parsed_ai,
        "sensor_readings": sensor_data,
        "capture_device": "USB Webcam (picamera2)",
        "scan_time": datetime.now().isoformat(),
    })

    sql = f"""
        INSERT INTO {DATABASE}.{SCHEMA}.{TABLE} (
            SCAN_TIMESTAMP, IMAGE_FILENAME, IMAGE_STAGE_PATH,
            QR_CODE_DATA, EXTRACTED_TEXT,
            PARSED_NAME, PARSED_TITLE, PARSED_COMPANY,
            PARSED_EMAIL, PARSED_PHONE,
            CONFIDENCE_SCORE, PROCESSING_STATUS,
            CREATED_BY, METADATA, NOTES, CONFERENCE_NAME
        )
        SELECT
            CURRENT_TIMESTAMP(), %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, PARSE_JSON(%s), %s, %s
    """

    cur = conn.cursor()
    try:
        cur.execute(sql, (
            filename,
            stage_path,
            qr_data,
            raw_ai,
            name,
            title,
            company,
            email,
            phone,
            0.9 if parsed_ai else 0.5,
            "PROCESSED",
            "badge_scanner_cli",
            metadata,
            notes,
            conference,
        ))
        print(f"  Inserted into {DATABASE}.{SCHEMA}.{TABLE}")

        # Get the inserted row ID
        cur.execute(f"SELECT MAX(ID) FROM {DATABASE}.{SCHEMA}.{TABLE}")
        row_id = cur.fetchone()[0]
        print(f"  Row ID: {row_id}")
        return row_id
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Step 6b: Store LLM result (thread-safe, opens own connection)
# ---------------------------------------------------------------------------

def store_llm_result(scan_id, model_name, llm_result, image_filename):
    """Insert one LLM result row into LOCAL_LLM_RESULTS.

    Opens its own Snowflake connection so it can be called safely from a
    background thread without sharing the main connection.
    """
    status = "SUCCESS" if llm_result.get("response") else "ERROR"
    error_msg = llm_result.get("error")
    if error_msg:
        status = "ERROR"

    metadata = json.dumps({
        "model": model_name,
        "scan_id": scan_id,
        "timestamp": datetime.now().isoformat(),
    })

    sql = f"""
        INSERT INTO {DATABASE}.{SCHEMA}.{LLM_RESULTS_TABLE} (
            SCAN_ID, MODEL_NAME, RESPONSE_TEXT, TOKENS,
            DURATION_S, STATUS, ERROR_MESSAGE, IMAGE_FILENAME, METADATA
        )
        SELECT %s, %s, %s, %s, %s, %s, %s, %s, PARSE_JSON(%s)
    """

    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, (
            scan_id,
            model_name,
            llm_result.get("response"),
            llm_result.get("tokens"),
            llm_result.get("duration_s"),
            status,
            error_msg,
            image_filename,
            metadata,
        ))
        print(f"  [{model_name}] Stored result in {LLM_RESULTS_TABLE} (scan_id={scan_id})")
    except Exception as e:
        print(f"  [{model_name}] ERROR storing LLM result: {e}")
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Step 6c: Run LLM and store (thread target)
# ---------------------------------------------------------------------------

def run_llm_and_store(model_name, scan_id, parsed_ai, sensor_data,
                      qr_results, image_path, image_filename):
    """Thread target: run inference for one model, then store the result."""
    try:
        result = analyze_with_local_llm(
            parsed_ai, sensor_data, qr_results,
            image_path=image_path, model=model_name,
        )
    except Exception as e:
        print(f"  [{model_name}] Unhandled error: {e}")
        result = {"response": None, "error": str(e)}

    store_llm_result(scan_id, model_name, result, image_filename)
    return result


# ---------------------------------------------------------------------------
# Step 7: Display results
# ---------------------------------------------------------------------------

def display_results(filename, stage_path, qr_results, raw_ai, parsed_ai,
                    row_id, sensor_data=None):
    """Print a formatted summary of all results."""
    print_section("SCAN RESULTS SUMMARY")

    print_field("Image File", filename)
    print_field("Stage Path", stage_path)
    print_field("Snowflake Row ID", str(row_id) if row_id else None)
    print()

    # Sensor readings
    if sensor_data and sensor_data.get("temperature_c") is not None:
        print("  Environmental Sensor (BMP280):")
        print(f"    Temperature : {sensor_data['temperature_c']} C / {sensor_data['temperature_f']} F")
        print(f"    Pressure    : {sensor_data['pressure_hpa']} hPa")
        print(f"    Est Altitude: {sensor_data['altitude_ft']} ft")
    elif sensor_data and sensor_data.get("error"):
        print(f"  Environmental Sensor: ERROR - {sensor_data['error']}")
    else:
        print("  Environmental Sensor: Not available")
    print()

    # QR Code results
    if qr_results:
        print("  QR / Barcode Data:")
        for r in qr_results:
            print(f"    - [{r['type']}] {r['data']}")
    else:
        print("  QR / Barcode Data: None detected")
    print()

    # AI-parsed badge info
    print("  Badge Information (AI-extracted):")
    if parsed_ai:
        print_field("Name", parsed_ai.get("name"), indent=4)
        print_field("Title", parsed_ai.get("title"), indent=4)
        print_field("Company", parsed_ai.get("company"), indent=4)
        print_field("Email", parsed_ai.get("email"), indent=4)
        print_field("Phone", parsed_ai.get("phone"), indent=4)
        print_field("Conference", parsed_ai.get("conference"), indent=4)
        print_field("Badge Type", parsed_ai.get("badge_type"), indent=4)
        other = parsed_ai.get("other_text")
        if other:
            print_field("Other Text", str(other), indent=4)
        summary = parsed_ai.get("summary")
        if summary:
            print(f"\n    Summary: {summary}")
    else:
        print("    (AI could not parse structured data)")
        if raw_ai:
            print(f"\n    Raw AI Response:\n    {raw_ai[:500]}")
    print()

    # Note about async LLM processing
    print(f"  Local LLM: Running async ({', '.join(LOCAL_LLM_MODELS)})")
    print(f"  Results will be stored in {DATABASE}.{SCHEMA}.{LLM_RESULTS_TABLE}")

    print(f"\n{'=' * 60}")
    print(f"  Done. Data stored in {DATABASE}.{SCHEMA}.{TABLE}")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("\n" + "=" * 60)
    print("  SNOWFLAKE SUMMIT BADGE SCANNER")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: Capture image
        filepath, filename = capture_image(tmpdir)

        # Step 2: Read BMP280 sensor
        sensor_data = read_bmp280_sensor()

        # Step 3: Scan QR codes
        qr_results = scan_qr_codes(filepath)

        # Step 3.5: Connect to Snowflake
        print_section("Connecting to Snowflake")
        conn = get_snowflake_connection()
        print(f"  Connection: {SNOWFLAKE_CONNECTION}")
        print(f"  Database: {DATABASE}.{SCHEMA}")
        print(f"  Warehouse: {WAREHOUSE}")

        try:
            stage_path = upload_to_stage(conn, filepath, filename)

            # Step 4: AI analysis (Cortex, cloud)
            raw_ai, parsed_ai = analyze_with_ai(conn, filename)

            # Step 5: Store metadata IMMEDIATELY (no LLM wait)
            row_id = store_metadata(
                conn, filename, stage_path, qr_results, raw_ai, parsed_ai,
                sensor_data=sensor_data,
            )

            # Step 6: Display results right away
            display_results(
                filename, stage_path, qr_results, raw_ai, parsed_ai, row_id,
                sensor_data=sensor_data,
            )
        finally:
            conn.close()

        # Step 7: Fork local LLM threads for each model (async)
        # Each thread opens its own Snowflake connection and stores to
        # LOCAL_LLM_RESULTS independently.  The tmpdir is still alive
        # because we're still inside the `with` block.
        print_section("STEP 7: Async Local LLM Inference")
        print(f"  Models : {', '.join(LOCAL_LLM_MODELS)}")
        print(f"  Timeout: {LOCAL_LLM_WAIT}s")
        print(f"  Scan ID: {row_id}")

        threads = []
        for model_name in LOCAL_LLM_MODELS:
            t = threading.Thread(
                target=run_llm_and_store,
                args=(model_name, row_id, parsed_ai, sensor_data,
                      qr_results, filepath, filename),
                daemon=True,
                name=f"llm-{model_name}",
            )
            t.start()
            threads.append((model_name, t))
            print(f"  Started thread: {t.name}")

        # Wait up to LOCAL_LLM_WAIT seconds for all threads
        deadline = time.time() + LOCAL_LLM_WAIT
        for model_name, t in threads:
            remaining = max(0, deadline - time.time())
            t.join(timeout=remaining)
            if t.is_alive():
                print(f"\n  [{model_name}] Exceeded {LOCAL_LLM_WAIT}s total wait -- abandoning.")
            else:
                print(f"  [{model_name}] Finished.")

        print(f"\n{'=' * 60}")
        print("  All done. Badge scan + LLM results stored.")
        print(f"  BADGE_SCANS row: {row_id}")
        print(f"  LLM results in: {DATABASE}.{SCHEMA}.{LLM_RESULTS_TABLE}")
        print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
