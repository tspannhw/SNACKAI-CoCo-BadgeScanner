#!/usr/bin/env python3
"""
Snowflake Summit Badge Scanner

End-to-end CLI pipeline that captures a conference badge image, reads
environmental sensor data, performs cloud and edge AI analysis, and
persists everything to Snowflake.

Pipeline steps:
  1. Capture  – USB webcam image via picamera2 (1920x1080 MJPEG).
  2. Sensor   – BMP280 temperature / pressure / altitude via I2C (smbus2).
  2b. Thermal – MLX90640 32x24 IR thermal frame via I2C (adafruit_mlx90640).
  3. QR Scan  – Decode barcodes and QR codes with pyzbar.
  3b. Upload  – PUT image to Snowflake internal stage (SNOWFLAKE_SSE).
  4. Cloud AI – Cortex COMPLETE (pixtral-large multimodal) extracts badge text.
  5. Edge AI  – Ollama moondream local vision LLM analyses the badge image.
  6. Store    – INSERT metadata (VARIANT JSON) into DEMO.DEMO.BADGE_SCANS.
  7. Display  – Print formatted results to the terminal.

Hardware requirements:
  - Raspberry Pi 5 (tested) with USB webcam (auto-detected via picamera2)
  - Tested cameras: Logitech C920, C922, C270, BRIO; generic UVC webcams
  - Pimoroni BMP280 on I2C bus 1, address 0x76
  - Pimoroni MLX90640 thermal camera on I2C bus 1, address 0x33
  - Ollama running locally (http://localhost:11434) with moondream pulled

Python dependencies:
  picamera2, pyzbar (+ libzbar0), Pillow, snowflake-connector-python,
  smbus2, adafruit-circuitpython-mlx90640, requests

Snowflake objects (DEMO.DEMO):
  - Stage:  BADGE_SCAN_STAGE  (internal, SNOWFLAKE_SSE, directory enabled)
  - Table:  BADGE_SCANS       (21 columns, ID IDENTITY primary key)

Configuration is at the top of this file (connection name, database,
stage, model names, I2C addresses, Ollama URL).

Usage:
  python3 badge_scanner.py                  # auto-select first USB camera
  python3 badge_scanner.py --camera 0       # use camera index 0 explicitly
  python3 badge_scanner.py --list-cameras   # list available cameras and exit
"""

import argparse
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

# MLX90640 thermal camera
MLX90640_I2C_ADDR = 0x33
MLX90640_REFRESH_RATE = 2  # Hz

# Local Ollama LLM
OLLAMA_URL = "http://localhost:11434"
LOCAL_LLM_MODEL = "moondream"
LOCAL_LLM_MODELS = ["moondream", "gemma4:e2b"]  # run both async
LOCAL_LLM_WAIT = 120  # seconds; wait for async LLM threads to finish
LLM_RESULTS_TABLE = "LOCAL_LLM_RESULTS"

# Slack notifications
SLACK_WEBHOOK_URL = "PLACEHOLDER"
SLACK_ENABLED = True

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


def send_to_slack(text, blocks=None):
    """Post a message to Slack via incoming webhook. Fails silently."""
    if not SLACK_ENABLED or not SLACK_WEBHOOK_URL:
        return
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"  [Slack] Warning: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"  [Slack] Warning: {e}")


def get_presigned_image_url(conn, filename):
    """Generate a temporary public URL for a staged image via Snowflake."""
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT GET_PRESIGNED_URL(@{DATABASE}.{SCHEMA}.{STAGE}, '{filename}', 3600)"
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"  [Slack] Presigned URL warning: {e}")
        return None


def notify_scan_results(conn, filename, parsed_ai, qr_results, sensor_data,
                        capture_meta, row_id, thermal_gif_name=None):
    """Send scan results to Slack with embedded badge image."""
    cam = capture_meta or {}
    ai = parsed_ai or {}
    name = ai.get("name", "Unknown")
    title = ai.get("title", "")
    company = ai.get("company", "")
    email = ai.get("email", "")
    cam_model = cam.get("camera_model", "Unknown")
    resolution = cam.get("resolution", "unknown")

    temp = ""
    if sensor_data and sensor_data.get("temperature_f") is not None:
        temp = f"{sensor_data['temperature_f']}F / {sensor_data['temperature_c']}C"

    qr_text = ""
    if qr_results:
        qr_text = ", ".join(r["data"][:80] for r in qr_results[:3])

    lines = [
        f"*Badge Scan Complete* (Row {row_id})",
        f"*Name:* {name}",
    ]
    if title:
        lines.append(f"*Title:* {title}")
    if company:
        lines.append(f"*Company:* {company}")
    if email:
        lines.append(f"*Email:* {email}")
    if qr_text:
        lines.append(f"*QR:* {qr_text}")
    if temp:
        lines.append(f"*Temp:* {temp}")
    thermal = sensor_data.get("thermal") if sensor_data else None
    if thermal and thermal.get("hotspot_temp_c") is not None:
        lines.append(f"*Thermal:* {thermal['hotspot_temp_c']}C hotspot, {thermal['human_pixels']} body pixels")
    lines.append(f"*Camera:* {cam_model} ({resolution})")
    lines.append(f"*Image:* {filename}")

    fallback_text = "\n".join(lines)

    # Build Block Kit blocks with embedded image
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": fallback_text},
        }
    ]

    image_url = get_presigned_image_url(conn, filename)
    if image_url:
        blocks.append({
            "type": "image",
            "image_url": image_url,
            "alt_text": f"Badge scan: {filename}",
            "title": {"type": "plain_text", "text": filename},
        })

    # Embed thermal heatmap GIF if available
    if thermal_gif_name:
        thermal_url = get_presigned_image_url(conn, thermal_gif_name)
        if thermal_url:
            blocks.append({
                "type": "image",
                "image_url": thermal_url,
                "alt_text": f"Thermal heatmap: {thermal_gif_name}",
                "title": {"type": "plain_text", "text": "Thermal Heatmap (MLX90640)"},
            })

    send_to_slack(fallback_text, blocks=blocks)


# ---------------------------------------------------------------------------
# Camera Discovery & Selection
# ---------------------------------------------------------------------------

def discover_cameras():
    """Return list of cameras detected by picamera2/libcamera.

    Each entry is a dict with keys: Num, Model, Id, and Location.
    USB cameras have 'usb' in their Id string.
    """
    return Picamera2.global_camera_info()


def select_camera(cameras, preferred_index=None):
    """Pick a camera index from the discovered list.

    If *preferred_index* is given, validate it exists and return it.
    Otherwise auto-select the first USB camera (Id contains 'usb'),
    falling back to camera 0 if no USB match is found.

    Returns (index, camera_info_dict).
    """
    if not cameras:
        raise RuntimeError("No cameras detected by picamera2")

    if preferred_index is not None:
        for cam in cameras:
            if cam["Num"] == preferred_index:
                return preferred_index, cam
        available = ", ".join(str(c["Num"]) for c in cameras)
        raise RuntimeError(
            f"Camera index {preferred_index} not found. Available: {available}"
        )

    # Auto-select: prefer USB cameras
    for cam in cameras:
        if "usb" in cam.get("Id", "").lower():
            return cam["Num"], cam

    # Fallback: first camera
    return cameras[0]["Num"], cameras[0]


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
# MLX90640 Thermal Camera Reading (adafruit-circuitpython-mlx90640)
# ---------------------------------------------------------------------------

def read_mlx90640_sensor():
    """Read a thermal frame from the MLX90640 32x24 IR sensor array.

    Returns summary statistics (min/max/mean/hotspot temperatures) and a
    count of pixels above 30 C (rough human-presence indicator).  The full
    768-element frame is included for downstream storage/analysis.
    """
    print_section("STEP 2b: Reading MLX90640 Thermal Camera")

    try:
        import board
        import busio
        import adafruit_mlx90640

        i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
        mlx = adafruit_mlx90640.MLX90640(i2c)
        mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ

        frame = [0] * 768
        # MLX90640 occasionally raises ValueError on read; retry a few times
        for _attempt in range(5):
            try:
                mlx.getFrame(frame)
                break
            except ValueError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Failed to read MLX90640 frame after 5 retries")

        # Compute summary statistics
        valid = [t for t in frame if t > -40]
        if not valid:
            raise RuntimeError("No valid temperature readings in frame")

        thermal_min = min(valid)
        thermal_max = max(valid)
        thermal_mean = sum(valid) / len(valid)
        human_pixels = sum(1 for t in valid if t > 30.0)

        print(f"  Frame       : 32x24 ({len(valid)} valid pixels)")
        print(f"  Min / Max   : {thermal_min:.1f} C / {thermal_max:.1f} C")
        print(f"  Mean        : {thermal_mean:.1f} C")
        print(f"  Hotspot     : {thermal_max:.1f} C / {thermal_max * 9/5 + 32:.1f} F")
        print(f"  Human pixels: {human_pixels}/768 (>30 C)")

        return {
            "thermal_min_c": round(thermal_min, 2),
            "thermal_max_c": round(thermal_max, 2),
            "thermal_mean_c": round(thermal_mean, 2),
            "hotspot_temp_c": round(thermal_max, 2),
            "hotspot_temp_f": round(thermal_max * 9 / 5 + 32, 2),
            "human_pixels": human_pixels,
            "frame_shape": [24, 32],
            "frame": [round(t, 1) for t in frame],
        }

    except Exception as e:
        print(f"  ERROR reading MLX90640 sensor: {e}")
        return {
            "thermal_min_c": None,
            "thermal_max_c": None,
            "thermal_mean_c": None,
            "hotspot_temp_c": None,
            "hotspot_temp_f": None,
            "human_pixels": None,
            "frame_shape": None,
            "frame": None,
            "error": str(e),
        }


def generate_thermal_gif(thermal_data, output_dir):
    """Generate a false-color thermal heatmap GIF from an MLX90640 frame.

    Uses an iron/heat colormap (black -> blue -> red -> yellow -> white)
    and bicubic upscaling from 32x24 to 320x240.
    Returns (filepath, filename) or (None, None) on failure.
    """
    frame = thermal_data.get("frame") if thermal_data else None
    if not frame or thermal_data.get("error"):
        return None, None

    try:
        import colorsys

        # Iron/heat colormap: maps 0.0-1.0 to thermal palette
        def thermal_color(val):
            """Map normalized value [0,1] to iron palette RGB tuple."""
            # Piecewise linear: black -> blue -> magenta -> red -> yellow -> white
            if val <= 0.0:
                return (0, 0, 0)
            elif val <= 0.2:
                t = val / 0.2
                return (0, 0, int(128 * t))
            elif val <= 0.4:
                t = (val - 0.2) / 0.2
                return (int(128 * t), 0, int(128 + 127 * (1 - t)))
            elif val <= 0.6:
                t = (val - 0.4) / 0.2
                return (int(128 + 127 * t), 0, int(128 * (1 - t)))
            elif val <= 0.8:
                t = (val - 0.6) / 0.2
                return (255, int(255 * t), 0)
            else:
                t = min((val - 0.8) / 0.2, 1.0)
                return (255, 255, int(255 * t))

        # Determine temperature range for normalization
        valid = [t for t in frame if t > -40]
        if not valid:
            return None, None
        t_min = min(valid)
        t_max = max(valid)
        t_range = t_max - t_min if t_max > t_min else 1.0

        # Build 32x24 image
        img = Image.new('RGB', (32, 24))
        for h in range(24):
            for w in range(32):
                temp = frame[h * 32 + w]
                normalized = max(0.0, min(1.0, (temp - t_min) / t_range))
                img.putpixel((w, h), thermal_color(normalized))

        # Upscale to 320x240 with bicubic interpolation
        img = img.resize((320, 240), Image.BICUBIC)

        # Save as GIF
        filename = f"thermal_{uuid.uuid4()}.gif"
        filepath = os.path.join(output_dir, filename)
        img.save(filepath)

        print(f"  Thermal GIF: {filename} (320x240)")
        return filepath, filename

    except Exception as e:
        print(f"  WARNING: Could not generate thermal GIF: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Step 1: Capture image from USB webcam
# ---------------------------------------------------------------------------

def capture_image(output_dir, camera_index=0, camera_info=None):
    """Capture a JPEG image from a USB webcam using picamera2.

    Adapts resolution to the camera's native capabilities.  Tries the
    camera's full sensor size first, then common fallbacks.
    Returns (filepath, filename, capture_meta) where capture_meta is a
    dict with camera model, resolution, and index.
    """
    print_section("STEP 1: Capturing Image from Webcam")

    model_name = (camera_info or {}).get("Model", "Unknown")
    print(f"  Camera: {model_name} (index {camera_index})")

    filename = f"badge_{uuid.uuid4()}.jpg"
    filepath = os.path.join(output_dir, filename)

    cam = Picamera2(camera_index)

    # Determine best resolution from camera properties
    props = cam.camera_properties
    native_size = props.get("PixelArraySize", (1920, 1080))
    # Preference list: native max, then common fallbacks
    resolutions = [
        native_size,
        (1920, 1080),
        (1280, 720),
        (640, 480),
    ]
    # De-duplicate while preserving order
    seen = set()
    unique_res = []
    for r in resolutions:
        if r not in seen:
            seen.add(r)
            unique_res.append(r)

    # Try each resolution until one works
    configured = False
    used_size = None
    for size in unique_res:
        try:
            config = cam.create_still_configuration(
                main={"format": "RGB888", "size": size}
            )
            cam.configure(config)
            configured = True
            used_size = size
            break
        except Exception:
            continue

    if not configured:
        # Last resort: let picamera2 choose defaults
        config = cam.create_still_configuration(main={"format": "RGB888"})
        cam.configure(config)
        used_size = config["main"]["size"]

    cam.start()
    # Let the camera auto-exposure settle
    time.sleep(2)
    cam.capture_file(filepath)
    cam.stop()
    cam.close()

    size_kb = os.path.getsize(filepath) / 1024
    print(f"  Captured image: {filename}")
    print(f"  Resolution: {used_size[0]}x{used_size[1]}")
    print(f"  Size: {size_kb:.1f} KB")
    print(f"  Local path: {filepath}")

    capture_meta = {
        "camera_model": model_name,
        "camera_index": camera_index,
        "resolution": f"{used_size[0]}x{used_size[1]}",
    }
    return filepath, filename, capture_meta


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

    thermal = sensor_data.get("thermal") if sensor_data else None
    if thermal and thermal.get("thermal_max_c") is not None:
        parts.append("Thermal camera readings (MLX90640 32x24 IR):")
        parts.append(f"  Hotspot: {thermal['hotspot_temp_c']} C / {thermal['hotspot_temp_f']} F")
        parts.append(f"  Scene range: {thermal['thermal_min_c']} C to {thermal['thermal_max_c']} C")
        parts.append(f"  Human-temp pixels (>30C): {thermal['human_pixels']}/768")

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
                    sensor_data=None, capture_meta=None):
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

    # Build capture device description from camera metadata
    cam_meta = capture_meta or {}
    cam_model = cam_meta.get("camera_model", "Unknown")
    cam_res = cam_meta.get("resolution", "unknown")
    capture_device = f"{cam_model} (picamera2, {cam_res})"

    metadata = json.dumps({
        "qr_codes": qr_results,
        "ai_model": AI_MODEL,
        "ai_raw_response": raw_ai,
        "ai_parsed": parsed_ai,
        "sensor_readings": sensor_data,
        "capture_device": capture_device,
        "camera": cam_meta,
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
                    row_id, sensor_data=None, thermal_gif_name=None):
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

    # Thermal camera readings
    thermal = sensor_data.get("thermal") if sensor_data else None
    if thermal and thermal.get("thermal_max_c") is not None:
        print("  Thermal Camera (MLX90640):")
        print(f"    Min / Max / Mean: {thermal['thermal_min_c']} C / {thermal['thermal_max_c']} C / {thermal['thermal_mean_c']} C")
        print(f"    Hotspot (body)  : {thermal['hotspot_temp_c']} C / {thermal['hotspot_temp_f']} F")
        print(f"    Human pixels    : {thermal['human_pixels']}/768 (>30 C)")
        if thermal_gif_name:
            print(f"    Thermal GIF     : {thermal_gif_name}")
    elif thermal and thermal.get("error"):
        print(f"  Thermal Camera: ERROR - {thermal['error']}")
    else:
        print("  Thermal Camera: Not available")
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

def main(camera_override=None):
    parser = argparse.ArgumentParser(
        description="Snowflake Summit Badge Scanner — capture, analyse, store.",
    )
    parser.add_argument(
        "--camera", type=int, default=None,
        help="Camera index to use (default: auto-select first USB camera)",
    )
    parser.add_argument(
        "--list-cameras", action="store_true",
        help="List available cameras and exit",
    )

    # When called programmatically (e.g. from manage.py), skip argparse to
    # avoid conflicts with the caller's argv.
    if camera_override is not None:
        preferred_camera = camera_override
        list_cameras = False
    else:
        args = parser.parse_args()
        preferred_camera = args.camera
        list_cameras = args.list_cameras

    # Discover cameras
    cameras = discover_cameras()

    if list_cameras:
        print(f"\nDetected {len(cameras)} camera(s):\n")
        for cam in cameras:
            is_usb = "usb" in cam.get("Id", "").lower()
            tag = " [USB]" if is_usb else ""
            print(f"  Index {cam['Num']}: {cam['Model']}{tag}")
            print(f"           Id: {cam['Id']}")
        if cameras:
            idx, info = select_camera(cameras, preferred_camera)
            print(f"\n  Auto-selected: index {idx} ({info['Model']})\n")
        else:
            print("  No cameras found.\n")
        return

    # Select camera
    cam_index, cam_info = select_camera(cameras, preferred_camera)

    print("\n" + "=" * 60)
    print("  SNOWFLAKE SUMMIT BADGE SCANNER")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Camera: {cam_info['Model']} (index {cam_index})")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: Capture image
        filepath, filename, capture_meta = capture_image(
            tmpdir, camera_index=cam_index, camera_info=cam_info,
        )

        # Step 2: Read BMP280 sensor
        sensor_data = read_bmp280_sensor()

        # Step 2b: Read MLX90640 thermal camera
        thermal_data = read_mlx90640_sensor()
        sensor_data["thermal"] = thermal_data

        # Step 2c: Generate thermal heatmap GIF
        thermal_gif_path, thermal_gif_name = generate_thermal_gif(thermal_data, tmpdir)

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

            # Upload thermal GIF if generated
            if thermal_gif_path:
                upload_to_stage(conn, thermal_gif_path, thermal_gif_name)

            # Step 4: AI analysis (Cortex, cloud)
            raw_ai, parsed_ai = analyze_with_ai(conn, filename)

            # Step 5: Store metadata IMMEDIATELY (no LLM wait)
            row_id = store_metadata(
                conn, filename, stage_path, qr_results, raw_ai, parsed_ai,
                sensor_data=sensor_data,
                capture_meta=capture_meta,
            )

            # Step 6: Display results right away
            display_results(
                filename, stage_path, qr_results, raw_ai, parsed_ai, row_id,
                sensor_data=sensor_data,
                thermal_gif_name=thermal_gif_name,
            )

            # Notify Slack
            notify_scan_results(
                conn, filename, parsed_ai, qr_results, sensor_data,
                capture_meta, row_id, thermal_gif_name=thermal_gif_name,
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
