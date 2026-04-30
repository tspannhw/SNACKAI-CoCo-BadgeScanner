# Snowflake Summit Badge Scanner

End-to-end CLI pipeline running on a Raspberry Pi 5 that captures conference
badge images, reads environmental sensor data, performs cloud and edge AI
analysis, and persists everything to Snowflake.

## Pipeline Steps

| Step | What | How |
|------|------|-----|
| 1. Capture | USB webcam photo (auto-resolution) | picamera2 with auto-detection |
| 2. Sensor | Temperature, pressure, altitude | BMP280 via I2C (smbus2) |
| 2b. Thermal | 32x24 IR thermal frame (hotspot, human detection) | MLX90640 via I2C (adafruit-circuitpython-mlx90640) |
| 3. QR Scan | Decode barcodes and QR codes | pyzbar + libzbar0 |
| 3b. Upload | PUT image to Snowflake internal stage | SNOWFLAKE_SSE encryption |
| 4. Cloud AI | Extract badge text (name, title, company) | Cortex COMPLETE (pixtral-large multimodal) |
| 5. Store | INSERT metadata row immediately | DEMO.DEMO.BADGE_SCANS |
| 6. Display | Print formatted results | Terminal output |
| 7. Edge AI | Async vision + text LLM inference | Ollama moondream + gemma4:e2b (parallel threads, both scan image) |
| 7b. Store LLM | INSERT each model's result | DEMO.DEMO.LOCAL_LLM_RESULTS |

## Hardware

- Raspberry Pi 5
- USB UVC webcam (tested: Logitech C920, C270; any libcamera-compatible USB camera should work)
- Pimoroni BMP280 breakout on I2C bus 1, address `0x76`
- Pimoroni MLX90640 thermal camera breakout on I2C bus 1, address `0x33`

## Software Prerequisites

| Component | Notes |
|-----------|-------|
| Python 3.11+ | Bookworm default |
| picamera2 | `apt install python3-picamera2` |
| libzbar0 | `apt install libzbar0` |
| pyzbar, Pillow | `pip install pyzbar Pillow` |
| snowflake-connector-python | `pip install snowflake-connector-python` |
| smbus2 | `pip install smbus2` |
| adafruit-circuitpython-mlx90640 | `pip install adafruit-circuitpython-mlx90640` |
| requests | `pip install requests` |
| Ollama | [ollama.com](https://ollama.com) with `ollama pull moondream && ollama pull gemma4:e2b` |

## Snowflake Setup

### Connection

Configure `~/.snowflake/connections.toml`:

```toml
[cortexcli1]
account   = "<account>"
user      = "<user>"
password  = "<password>"
database  = "DEMO"
schema    = "DEMO"
warehouse = "INGEST"
```

### Objects (DEMO.DEMO)

**Stage** -- internal, SNOWFLAKE_SSE, directory enabled:

```sql
CREATE OR REPLACE STAGE BADGE_SCAN_STAGE
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  DIRECTORY = (ENABLE = TRUE);
```

**Table** -- 21 columns:

```sql
CREATE OR REPLACE TABLE BADGE_SCANS (
    ID                 NUMBER(38,0) IDENTITY PRIMARY KEY,
    SCAN_TIMESTAMP     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    IMAGE_FILENAME     VARCHAR(500),
    IMAGE_STAGE_PATH   VARCHAR(1000),
    QR_CODE_DATA       VARCHAR,
    EXTRACTED_TEXT      VARCHAR,
    PARSED_NAME        VARCHAR(200),
    PARSED_TITLE       VARCHAR(200),
    PARSED_COMPANY     VARCHAR(200),
    PARSED_EMAIL       VARCHAR(200),
    PARSED_PHONE       VARCHAR(50),
    CONFIDENCE_SCORE   FLOAT,
    PROCESSING_STATUS  VARCHAR(50) DEFAULT 'PENDING',
    CREATED_BY         VARCHAR(100),
    METADATA           VARIANT,
    NOTES              VARCHAR,
    SECOND_EMAIL       VARCHAR(200),
    SECOND_PHONE       VARCHAR(50),
    PERSON_PHOTO_PATH  VARCHAR(1000),
    CONFERENCE_NAME    VARCHAR(200),
    BADGE_IMAGE_URL    VARCHAR(2000)
);
```

SNOWFLAKE_SSE encryption is required for `TO_FILE()` used by Cortex multimodal.

**LLM Results Table** -- async local LLM results with FK back to BADGE_SCANS:

```sql
CREATE OR REPLACE TABLE LOCAL_LLM_RESULTS (
    ID              NUMBER(38,0) IDENTITY PRIMARY KEY,
    SCAN_ID         NUMBER(38,0),
    MODEL_NAME      VARCHAR(100),
    RESPONSE_TEXT   VARCHAR,
    TOKENS          NUMBER(38,0),
    DURATION_S      FLOAT,
    STATUS          VARCHAR(50),
    ERROR_MESSAGE   VARCHAR,
    IMAGE_FILENAME  VARCHAR(500),
    METADATA        VARIANT,
    CREATED_AT      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
```

## Files

| File | Lines | Description |
|------|-------|-------------|
| `badge_scanner.py` | ~1006 | Main pipeline with multi-camera support and async LLM threads |
| `manage.py` | ~536 | Management CLI (start/stop/scan/list/test/validate) |
| `test_ollama.py` | 212 | Standalone Ollama vision test tool |

## Usage

### Quick Start

```bash
# 1. Start services and check dependencies
python3 manage.py start

# 2. Run pre-flight health checks
python3 manage.py test

# 3. List available cameras
python3 badge_scanner.py --list-cameras

# 4. Scan a badge (auto-selects first USB camera)
python3 manage.py scan
# or with a specific camera:
python3 manage.py scan --camera 0
# or directly:
python3 badge_scanner.py
python3 badge_scanner.py --camera 0

# 5. List recent scans
python3 manage.py list
python3 manage.py list --limit 25
```

### Management CLI (`manage.py`)

```
python3 manage.py <command>

Commands:
  start      Start Ollama, check/install libzbar0 and pyzbar
  stop       Stop the Ollama service
  scan       Run the full badge_scanner.py pipeline (--camera N to pick camera)
  list       Query BADGE_SCANS and display recent rows (--limit N)
  test       11 pre-flight health checks (camera, sensor, Ollama, Snowflake, deps)
  validate   5 static code checks (syntax, AST, schema, pipeline, bind params)
```

### Standalone Ollama Test (`test_ollama.py`)

Test the local moondream vision model on any image:

```bash
python3 test_ollama.py photo.jpg                    # output: photo.jpg.ollama.json
python3 test_ollama.py photo.jpg -o result.json     # custom output path
python3 test_ollama.py photo.jpg --timeout 30       # custom timeout
```

## Architecture

```
USB Webcam(s) ──> picamera2 ──> JPEG file ──> pyzbar (QR decode)
  (auto-detect)        │
                       │  native resolution
BMP280 (I2C) ──> smbus2 ──────>│
                                │
                         badge_scanner.py
                           /          \
                  Snowflake             (immediate)
                  Cortex AI
                (pixtral-large)
                     │
              BADGE_SCANS ─── INSERT row (step 5)
                     │
              display results (step 6)
                     │
              fork daemon threads (step 7)
               /              \
          moondream        gemma4:e2b
         (vision)       (vision+thinking)
              \              /
          LOCAL_LLM_RESULTS
        (each thread stores independently)
```

### METADATA VARIANT Structure (BADGE_SCANS)

Each row's `METADATA` column stores a JSON object:

```json
{
  "qr_codes": [{"type": "QRCODE", "data": "..."}],
  "ai_model": "pixtral-large",
  "ai_raw_response": "...",
  "ai_parsed": {
    "name": "...",
    "title": "...",
    "company": "...",
    "email": "...",
    "confidence": 0.95
  },
  "sensor_readings": {
    "temperature_c": 24.5,
    "temperature_f": 76.1,
    "pressure_hpa": 1013.2,
    "altitude_ft": 30.5
  },
  "camera": {
    "camera_model": "HD Pro Webcam C920",
    "camera_index": 0,
    "resolution": "2304x1536"
  },
  "capture_device": "HD Pro Webcam C920 (picamera2, 2304x1536)",
  "scan_time": "2025-06-10T14:30:00"
}
```

Local LLM results are stored separately in the `LOCAL_LLM_RESULTS` table, not in
this VARIANT column. Each LLM thread writes its own row with its own Snowflake
connection.

### Async Local LLM

After storing to Snowflake and displaying results, the pipeline forks one daemon
thread per model in `LOCAL_LLM_MODELS` (default: moondream + gemma4:e2b). Both
models receive the base64-encoded badge image and run in parallel. The main
process waits up to `LOCAL_LLM_WAIT` seconds (default 300, i.e. 5 minutes)
total -- the deadline is shared, so if moondream finishes in 60s, gemma4:e2b
gets the remaining 240s. Any thread still running after the deadline is
abandoned (daemon threads exit when the process exits).

Each thread opens its own Snowflake connection and INSERTs to
`LOCAL_LLM_RESULTS` independently, avoiding contention on the main connection.

## Configuration

All configuration constants are at the top of each file:

| Constant | File | Default | Description |
|----------|------|---------|-------------|
| `SNOWFLAKE_CONNECTION` | badge_scanner.py | `cortexcli1` | Connection name |
| `DATABASE` / `SCHEMA` | badge_scanner.py | `DEMO` / `DEMO` | Target DB objects |
| `STAGE` | badge_scanner.py | `BADGE_SCAN_STAGE` | Internal stage name |
| `AI_MODEL` | badge_scanner.py | `pixtral-large` | Cortex multimodal model |
| `LOCAL_LLM_MODEL` | badge_scanner.py | `moondream` | Default Ollama model |
| `LOCAL_LLM_MODELS` | badge_scanner.py | `["moondream", "gemma4:e2b"]` | Models to run async |
| `LOCAL_LLM_WAIT` | badge_scanner.py | `300` | Total seconds (5 min) to wait for LLM threads |
| `LLM_RESULTS_TABLE` | badge_scanner.py | `LOCAL_LLM_RESULTS` | Table for async LLM results |
| `OLLAMA_URL` | badge_scanner.py | `http://localhost:11434` | Ollama API endpoint |
| `BMP280_I2C_BUS` | badge_scanner.py | `1` | I2C bus number |
| `BMP280_I2C_ADDR` | badge_scanner.py | `0x76` | BMP280 I2C address |
| `SLACK_WEBHOOK_URL` | both | (set) | Slack incoming webhook URL |
| `SLACK_ENABLED` | both | `True` | Set `False` to disable Slack notifications |

### Multi-Camera Support

The scanner auto-detects cameras via `Picamera2.global_camera_info()` (libcamera).
USB UVC cameras are preferred over CSI cameras. Resolution is negotiated
automatically — the scanner tries native sensor resolution first, then falls back
through 1920x1080, 1280x720, and 640x480.

```bash
# List detected cameras
python3 badge_scanner.py --list-cameras

# Use a specific camera by index
python3 badge_scanner.py --camera 1
python3 manage.py scan --camera 1
```

**Tested cameras:**

| Camera | Interface | Native Resolution |
|--------|-----------|-------------------|
| Logitech C920 (HD Pro) | USB | 2304x1536 |
| Logitech C270 | USB | 1280x720 |
| Raspberry Pi Camera Module | CSI | varies |

Camera model, index, and capture resolution are stored in the `METADATA` VARIANT
column under the `camera` key for every scan.

### Slack Notifications

All pipeline outputs are sent to Slack via an incoming webhook:

| Event | Message |
|-------|---------|
| `manage.py scan` | Badge scan results (name, company, camera, temp, QR data) + **scanned image** |
| `manage.py test` | Health check pass/fail summary |
| `manage.py validate` | Static validation pass/fail summary |

Scan notifications embed the captured badge image directly in Slack using a
Snowflake pre-signed URL (`GET_PRESIGNED_URL`, 1-hour expiry). The image
appears inline below the scan metadata.

To disable, set `SLACK_ENABLED = False` in both `badge_scanner.py` and `manage.py`.
To change the channel, update `SLACK_WEBHOOK_URL` in both files.
