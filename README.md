# Snowflake Summit Badge Scanner

End-to-end CLI pipeline running on a Raspberry Pi 5 that captures conference
badge images, reads environmental sensor data, performs cloud and edge AI
analysis, and persists everything to Snowflake.

## Example Run

````

root@rp500:/opt/demo/badgescanner# python badge_scanner.py 
[0:35:02.955272010] [3678]  INFO Camera camera_manager.cpp:340 libcamera v0.7.0+rpt20260205
[0:35:02.964385042] [3684]  INFO Camera camera_manager.cpp:223 Adding camera '/base/axi/pcie@1000120000/rp1/usb@200000-1:1.0-046d:0892' for pipeline handler uvcvideo

============================================================
  SNOWFLAKE SUMMIT BADGE SCANNER
  2026-04-28 17:21:28
  Camera: HD Pro Webcam C920 (index 0)
============================================================

============================================================
  STEP 1: Capturing Image from Webcam
============================================================
  Camera: HD Pro Webcam C920 (index 0)
[0:35:02.976835429] [3678]  INFO Camera camera.cpp:1215 configuring streams: (0) 1920x1080-MJPEG/Rec709/Rec709/Rec601/Limited
[0:35:03.629797279] [3684]  INFO V4L2 v4l2_videodevice.cpp:1913 /dev/video0[10:cap]: Zero sequence expected for first frame (got 1)
  Captured image: badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg
  Resolution: 2304x1536
  Size: 176.1 KB
  Local path: /tmp/tmpw9c1551c/badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg

============================================================
  STEP 2: Reading BMP280 Sensor
============================================================
  Temperature : 25.5 C / 77.8 F
  Pressure    : 1006.6 hPa
  Est Altitude: 181 ft

============================================================
  STEP 3: Scanning QR Codes & Barcodes
============================================================
  Found 1 code(s):
    [1] Type: QRCODE
        Data: https://ip.bizzabo.com/events/807411/attendees/30856680

============================================================
  Connecting to Snowflake
============================================================
  Connection: cortexcli1
  Database: DEMO.DEMO
  Warehouse: INGEST

============================================================
  STEP 3b: Uploading Image to Snowflake Stage
============================================================
  File: badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg
  Stage: @DEMO.DEMO.BADGE_SCAN_STAGE
  Status: UPLOADED
  Directory refreshed.

============================================================
  STEP 4: Running Cortex AI Analysis
============================================================
  Model: pixtral-large
  Analyzing: badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg
  Waiting for AI response...
  Response received (406 chars)

============================================================
  STEP 6: Storing Metadata in Snowflake
============================================================
  Inserted into DEMO.DEMO.BADGE_SCANS
  Row ID: 2301

============================================================
  SCAN RESULTS SUMMARY
============================================================
  Image File          : badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg
  Stage Path          : @DEMO.DEMO.BADGE_SCAN_STAGE/badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg
  Snowflake Row ID    : 2301

  Environmental Sensor (BMP280):
    Temperature : 25.47 C / 77.84 F
    Pressure    : 1006.63 hPa
    Est Altitude: 181.2 ft

  QR / Barcode Data:
    - [QRCODE] https://ip.bizzabo.com/events/807411/attendees/30856680

  Badge Information (AI-extracted):
    Name                : Timothy Spann
    Title               : Sr Solution Engineer
    Company             : Snowflake
    Email               : (not detected)
    Phone               : (not detected)
    Conference          : DATA FOR BREAKFAST
    Badge Type          : Employee

    Summary: The badge is for Timothy Spann, a Sr Solution Engineer at Snowflake, attending the DATA FOR BREAKFAST conference. The badge type is Employee and includes a QR code.

  Local LLM: Running async (moondream, gemma4:e2b)
  Results will be stored in DEMO.DEMO.LOCAL_LLM_RESULTS

============================================================
  Done. Data stored in DEMO.DEMO.BADGE_SCANS
============================================================


============================================================
  STEP 7: Async Local LLM Inference
============================================================
  Models : moondream, gemma4:e2b
  Timeout: 300s
  Scan ID: 2301

============================================================
  Local LLM Analysis (moondream via Ollama)
  Started thread: llm-moondream
============================================================

============================================================
  Local LLM Analysis (gemma4:e2b via Ollama)
============================================================
  Started thread: llm-gemma4:e2b
  Image : /tmp/tmpw9c1551c/badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg (base64, 234 KB)
  Model : moondream
  Server: http://localhost:11434
  Waiting for local LLM response...
  Image : /tmp/tmpw9c1551c/badge_ef5b3cfe-6a22-41ba-b0c1-f1941a69d2c3.jpg (base64, 234 KB)
  Model : gemma4:e2b
  Server: http://localhost:11434
  Waiting for local LLM response...
  Model loaded.
  Response received (51 chars, 18 tokens, 85.5s)
  ---
  ids.timothy.spann.solutionengineer at snowflake.com
  [moondream] Stored result in LOCAL_LLM_RESULTS (scan_id=2301)
  [moondream] Finished.
  Model loaded.

  [gemma4:e2b] Exceeded 300s total wait -- abandoning.

============================================================
  All done. Badge scan + LLM results stored.
  BADGE_SCANS row: 2301
  LLM results in: DEMO.DEMO.LOCAL_LLM_RESULTS
============================================================

root@rp500:/opt/demo/badgescanner# 

````



## Pipeline Steps

| Step | What | How |
|------|------|-----|
| 1. Capture | USB webcam photo (1920x1080 MJPEG) | fswebcam `/dev/video0` |
| 2. Sensor | Temperature, pressure, altitude | BMP280 via I2C (smbus2) |
| 3. QR Scan | Decode barcodes and QR codes | pyzbar + libzbar0 |
| 3b. Upload | PUT image to Snowflake internal stage | SNOWFLAKE_SSE encryption |
| 4. Cloud AI | Extract badge text (name, title, company) | Cortex COMPLETE (pixtral-large multimodal) |
| 5. Store | INSERT metadata row immediately | DEMO.DEMO.BADGE_SCANS |
| 6. Display | Print formatted results | Terminal output |
| 7. Edge AI | Async vision + text LLM inference | Ollama moondream + gemma4:e2b (parallel threads) |
| 7b. Store LLM | INSERT each model's result | DEMO.DEMO.LOCAL_LLM_RESULTS |

## Hardware

- Raspberry Pi 5
- USB webcam at `/dev/video0`
- Pimoroni BMP280 breakout on I2C bus 1, address `0x76`

## Software Prerequisites

| Component | Notes |
|-----------|-------|
| Python 3.11+ | Bookworm default |
| picamera2 | `apt install python3-picamera2` |
| libzbar0 | `apt install libzbar0` |
| pyzbar, Pillow | `pip install pyzbar Pillow` |
| snowflake-connector-python | `pip install snowflake-connector-python` |
| smbus2 | `pip install smbus2` |
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
| `badge_scanner.py` | 854 | Main pipeline with async LLM threads |
| `manage.py` | 514 | Management CLI (start/stop/scan/list/test/validate) |
| `test_ollama.py` | 212 | Standalone Ollama vision test tool |

## Usage

### Quick Start

```bash
# 1. Start services and check dependencies
python3 manage.py start

# 2. Run pre-flight health checks
python3 manage.py test

# 3. Scan a badge (full pipeline)
python3 manage.py scan
# or directly:
python3 badge_scanner.py

# 4. List recent scans
python3 manage.py list
python3 manage.py list --limit 25
```

### Management CLI (`manage.py`)

```
python3 manage.py <command>

Commands:
  start      Start Ollama, check/install libzbar0 and pyzbar
  stop       Stop the Ollama service
  scan       Run the full badge_scanner.py pipeline
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
USB Webcam ──> picamera2 ──> JPEG file ──> pyzbar (QR decode)
                                │
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
         (vision)         (text-only)
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
  "capture_device": "USB Webcam (picamera2)",
  "scan_time": "2025-06-10T14:30:00"
}
```

Local LLM results are stored separately in the `LOCAL_LLM_RESULTS` table, not in
this VARIANT column. Each LLM thread writes its own row with its own Snowflake
connection.

### Async Local LLM

After storing to Snowflake and displaying results, the pipeline forks one daemon
thread per model in `LOCAL_LLM_MODELS` (default: moondream + gemma4:e2b). Both
threads run in parallel. The main process waits up to `LOCAL_LLM_WAIT` seconds
(default 180) total -- the deadline is shared, so if moondream finishes in 60s,
gemma4:e2b gets the remaining 120s. Any thread still running after the deadline
is abandoned (daemon threads exit when the process exits).

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
| `LOCAL_LLM_WAIT` | badge_scanner.py | `180` | Total seconds to wait for LLM threads |
| `LLM_RESULTS_TABLE` | badge_scanner.py | `LOCAL_LLM_RESULTS` | Table for async LLM results |
| `OLLAMA_URL` | badge_scanner.py | `http://localhost:11434` | Ollama API endpoint |
| `BMP280_I2C_BUS` | badge_scanner.py | `1` | I2C bus number |
| `BMP280_I2C_ADDR` | badge_scanner.py | `0x76` | BMP280 I2C address |
| `CAMERA_INDEX` | badge_scanner.py | `0` | `/dev/videoN` index |



## Newest Run

````

root@rp500:/opt/demo/badgescanner# python3 badge_scanner.py 
[0:54:36.752149243] [3784]  INFO Camera camera_manager.cpp:340 libcamera v0.7.0+rpt20260205
[0:54:36.760384694] [3790]  INFO Camera camera_manager.cpp:223 Adding camera '/base/axi/pcie@1000120000/rp1/usb@200000-1:1.0-046d:0892' for pipeline handler uvcvideo

============================================================
  SNOWFLAKE SUMMIT BADGE SCANNER
  2026-04-29 14:36:56
  Camera: HD Pro Webcam C920 (index 0)
============================================================

============================================================
  STEP 1: Capturing Image from Webcam
============================================================
  Camera: HD Pro Webcam C920 (index 0)
[0:54:36.769921641] [3784]  INFO Camera camera.cpp:1215 configuring streams: (0) 1920x1080-MJPEG/Rec709/Rec709/Rec601/Limited
[0:54:37.353726565] [3790]  INFO V4L2 v4l2_videodevice.cpp:1913 /dev/video0[10:cap]: Zero sequence expected for first frame (got 1)
  Captured image: badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg
  Resolution: 2304x1536
  Size: 189.2 KB
  Local path: /tmp/tmp54a7egi7/badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg

============================================================
  STEP 2: Reading BMP280 Sensor
============================================================
  Temperature : 24.7 C / 76.4 F
  Pressure    : 1001.0 hPa
  Est Altitude: 336 ft

============================================================
  STEP 3: Scanning QR Codes & Barcodes
============================================================
  No QR codes or barcodes detected.

============================================================
  Connecting to Snowflake
============================================================
  Connection: cortexcli1
  Database: DEMO.DEMO
  Warehouse: INGEST

============================================================
  STEP 3b: Uploading Image to Snowflake Stage
============================================================
  File: badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg
  Stage: @DEMO.DEMO.BADGE_SCAN_STAGE
  Status: UPLOADED
  Directory refreshed.

============================================================
  STEP 4: Running Cortex AI Analysis
============================================================
  Model: pixtral-large
  Analyzing: badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg
  Waiting for AI response...
  Response received (406 chars)

============================================================
  STEP 6: Storing Metadata in Snowflake
============================================================
  Inserted into DEMO.DEMO.BADGE_SCANS
  Row ID: 3101

============================================================
  SCAN RESULTS SUMMARY
============================================================
  Image File          : badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg
  Stage Path          : @DEMO.DEMO.BADGE_SCAN_STAGE/badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg
  Snowflake Row ID    : 3101

  Environmental Sensor (BMP280):
    Temperature : 24.67 C / 76.4 F
    Pressure    : 1001.01 hPa
    Est Altitude: 336.0 ft

  QR / Barcode Data: None detected

  Badge Information (AI-extracted):
    Name                : Timothy Spann
    Title               : (not detected)
    Company             : Systemative
    Email               : (not detected)
    Phone               : (not detected)
    Conference          : DevNexus 22
    Badge Type          : Gold Sponsor
    Other Text          : Microsoft Azure

    Summary: The badge is from the DevNexus 22 conference, indicating Timothy Spann from Systemative as a Gold Sponsor. The badge also includes a QR code and the Microsoft Azure logo.

  Local LLM: Running async (moondream, gemma4:e2b)
  Results will be stored in DEMO.DEMO.LOCAL_LLM_RESULTS

============================================================
  Done. Data stored in DEMO.DEMO.BADGE_SCANS
============================================================


============================================================
  STEP 7: Async Local LLM Inference
============================================================
  Models : moondream, gemma4:e2b
  Timeout: 300s
  Scan ID: 3101

============================================================
  Started thread: llm-moondream
  Local LLM Analysis (moondream via Ollama)
============================================================

============================================================
  Started thread: llm-gemma4:e2b
  Local LLM Analysis (gemma4:e2b via Ollama)
============================================================
  Image : /tmp/tmp54a7egi7/badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg (base64, 252 KB)
  Model : moondream
  Server: http://localhost:11434
  Waiting for local LLM response...
  Image : /tmp/tmp54a7egi7/badge_84a78070-6c3d-4638-aed3-7cd6808cae85.jpg (base64, 252 KB)
  Model : gemma4:e2b
  Server: http://localhost:11434
  Waiting for local LLM response...
  Model loaded.




````
