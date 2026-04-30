-- ============================================================
-- Badge Scanner — Snowflake Object Setup
-- ============================================================
-- Re-runnable DDL for all objects in DEMO.DEMO used by the
-- Badge Scanner pipeline (badge_scanner.py, manage.py).
--
-- Run with:
--   snowsql -c cortexcli1 -f setup.sql
--   -- or --
--   python3 -c "
--     import snowflake.connector
--     conn = snowflake.connector.connect(connection_name='cortexcli1')
--     with open('setup.sql') as f:
--         for stmt in f.read().split(';'):
--             if stmt.strip():
--                 conn.cursor().execute(stmt)
--   "
--
-- Objects created:
--   Database:    DEMO
--   Schema:      DEMO.DEMO
--   Warehouse:   INGEST (X-Small)
--   Stages:      BADGE_SCAN_STAGE, BADGE_IMAGES_STAGE, BADGE_IMAGES
--   Tables:      BADGE_SCANS, LOCAL_LLM_RESULTS, PROCESSING_LOGS,
--                BADGE_DATA, BADGE_IMAGES
--   Views:       BADGE_ANALYTICS
--   Functions:   PARSE_QR_VCARD, EXTRACT_TEXT_DOCUMENT_AI
--   Procedures:  PROCESS_BADGE, PROCESS_BADGE_IMAGE
-- ============================================================

-- ============================================================
-- 1. DATABASE, SCHEMA, WAREHOUSE
-- ============================================================

CREATE DATABASE IF NOT EXISTS DEMO;
CREATE SCHEMA IF NOT EXISTS DEMO.DEMO;

USE DATABASE DEMO;
USE SCHEMA DEMO.DEMO;

CREATE WAREHOUSE IF NOT EXISTS INGEST
    WAREHOUSE_SIZE   = 'X-SMALL'
    AUTO_SUSPEND     = 60
    AUTO_RESUME      = TRUE
    INITIALLY_SUSPENDED = TRUE;

USE WAREHOUSE INGEST;

-- ============================================================
-- 2. STAGES
-- ============================================================

-- Primary stage used by badge_scanner.py for image uploads.
-- Internal, no client-side encryption, directory table enabled
-- for GET_PRESIGNED_URL and Cortex AI TO_FILE access.
CREATE STAGE IF NOT EXISTS BADGE_SCAN_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    DIRECTORY = (ENABLE = TRUE)
    COMMENT = 'Badge scanner images with SSE encryption for Cortex AI analysis';

-- Stage used by PROCESS_BADGE_IMAGE stored procedure for
-- server-side AI_EXTRACT processing.
CREATE STAGE IF NOT EXISTS BADGE_IMAGES_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_FULL')
    DIRECTORY = (ENABLE = TRUE)
    COMMENT = 'Stage for storing badge images and processing files';

-- Legacy image stage (directory enabled, no CSE).
CREATE STAGE IF NOT EXISTS BADGE_IMAGES
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    DIRECTORY = (ENABLE = TRUE);

-- ============================================================
-- 3. TABLES
-- ============================================================

-- Primary scan results table (21 columns).
-- Used by: badge_scanner.py (INSERT), manage.py (SELECT),
--          PROCESS_BADGE procedure, PROCESS_BADGE_IMAGE procedure.
CREATE TABLE IF NOT EXISTS BADGE_SCANS (
    ID                  NUMBER(38,0) NOT NULL AUTOINCREMENT START 1 INCREMENT 1 NOORDER,
    SCAN_TIMESTAMP      TIMESTAMP_NTZ(9) DEFAULT CURRENT_TIMESTAMP(),
    IMAGE_FILENAME      VARCHAR(500),
    IMAGE_STAGE_PATH    VARCHAR(1000),
    QR_CODE_DATA        VARCHAR(16777216),
    EXTRACTED_TEXT      VARCHAR(16777216),
    PARSED_NAME         VARCHAR(200),
    PARSED_TITLE        VARCHAR(200),
    PARSED_COMPANY      VARCHAR(200),
    PARSED_EMAIL        VARCHAR(200),
    PARSED_PHONE        VARCHAR(50),
    CONFIDENCE_SCORE    FLOAT,
    PROCESSING_STATUS   VARCHAR(50) DEFAULT 'PENDING',
    CREATED_BY          VARCHAR(100),
    METADATA            VARIANT,
    NOTES               VARCHAR(16777216),
    SECOND_EMAIL        VARCHAR(200),
    SECOND_PHONE        VARCHAR(50),
    PERSON_PHOTO_PATH   VARCHAR(1000),
    CONFERENCE_NAME     VARCHAR(200),
    BADGE_IMAGE_URL     VARCHAR(2000),
    PRIMARY KEY (ID)
);

-- Local LLM inference results (Ollama moondream / gemma4).
-- Used by: badge_scanner.py (INSERT via store_llm_result),
--          manage.py dashboard (SELECT for LLM metrics).
CREATE TABLE IF NOT EXISTS LOCAL_LLM_RESULTS (
    ID              NUMBER(38,0) NOT NULL AUTOINCREMENT START 1 INCREMENT 1 NOORDER,
    SCAN_ID         NUMBER(38,0),
    MODEL_NAME      VARCHAR(100),
    RESPONSE_TEXT   VARCHAR(16777216),
    TOKENS          NUMBER(38,0),
    DURATION_S      FLOAT,
    STATUS          VARCHAR(50),
    ERROR_MESSAGE   VARCHAR(16777216),
    IMAGE_FILENAME  VARCHAR(500),
    METADATA        VARIANT,
    CREATED_AT      TIMESTAMP_NTZ(9) DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (ID)
);

-- Processing audit log for stored procedure executions.
-- Used by: PROCESS_BADGE stored procedure.
CREATE TABLE IF NOT EXISTS PROCESSING_LOGS (
    ID                  NUMBER(38,0) NOT NULL AUTOINCREMENT START 1 INCREMENT 1 NOORDER,
    BADGE_SCAN_ID       NUMBER(38,0),
    OPERATION           VARCHAR(100),
    OPERATION_TIMESTAMP TIMESTAMP_NTZ(9) DEFAULT CURRENT_TIMESTAMP(),
    STATUS              VARCHAR(50),
    DETAILS             VARCHAR(16777216),
    EXECUTION_TIME_MS   NUMBER(38,0),
    ERROR_MESSAGE       VARCHAR(16777216),
    PRIMARY KEY (ID),
    FOREIGN KEY (BADGE_SCAN_ID) REFERENCES BADGE_SCANS(ID)
);

-- Legacy badge binary storage (unused by current pipeline).
CREATE TABLE IF NOT EXISTS BADGE_DATA (
    BADGE_ID              NUMBER(38,0) AUTOINCREMENT START 1 INCREMENT 1 NOORDER,
    IMAGE_ID              NUMBER(38,0),
    QR_CODE_DATA          VARCHAR(16777216),
    EXTRACTED_TEXT        VARCHAR(16777216),
    PROCESSING_TIMESTAMP  TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP()
);

-- Legacy image binary storage (unused by current pipeline).
CREATE TABLE IF NOT EXISTS BADGE_IMAGES (
    IMAGE_ID          NUMBER(38,0) AUTOINCREMENT START 1 INCREMENT 1 NOORDER,
    CONFERENCE_NAME   VARCHAR(16777216),
    UPLOAD_TIMESTAMP  TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
    IMAGE_BINARY      BINARY(8388608),
    PROCESSED_FLAG    BOOLEAN DEFAULT FALSE
);

-- ============================================================
-- 4. VIEWS
-- ============================================================

-- Aggregated daily analytics used by dashboards and reporting.
CREATE OR REPLACE VIEW BADGE_ANALYTICS AS
SELECT
    COUNT(*) AS TOTAL_SCANS,
    COUNT(DISTINCT PARSED_COMPANY) AS UNIQUE_COMPANIES,
    AVG(CONFIDENCE_SCORE) AS AVG_CONFIDENCE,
    COUNT(CASE WHEN QR_CODE_DATA IS NOT NULL AND QR_CODE_DATA != '[]' THEN 1 END) AS SCANS_WITH_QR,
    COUNT(CASE WHEN CONFIDENCE_SCORE >= 80 THEN 1 END) AS HIGH_CONFIDENCE_SCANS,
    DATE_TRUNC('day', SCAN_TIMESTAMP) AS SCAN_DATE,
    COUNT(*) AS DAILY_SCAN_COUNT
FROM BADGE_SCANS
GROUP BY DATE_TRUNC('day', SCAN_TIMESTAMP)
ORDER BY SCAN_DATE DESC;

-- ============================================================
-- 5. FUNCTIONS (UDFs)
-- ============================================================

-- Parse QR code text (vCard, JSON, MECARD, URL, plain text)
-- into structured contact fields.
-- Used by: PROCESS_BADGE_IMAGE stored procedure.
CREATE OR REPLACE FUNCTION PARSE_QR_VCARD(QR_TEXT VARCHAR)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
HANDLER = 'parse_qr'
AS '
import json
import re

def parse_qr(qr_text: str) -> dict:
    result = {
        "raw_data": qr_text,
        "format": "UNKNOWN",
        "name": None,
        "title": None,
        "company": None,
        "email": None,
        "phone": None,
        "url": None
    }
    if not qr_text:
        return result
    text = qr_text.strip()
    # Try vCard
    if text.upper().startswith("BEGIN:VCARD"):
        result["format"] = "VCARD"
        fn = re.search(r"FN[;:](.+)", text, re.IGNORECASE)
        if fn:
            result["name"] = fn.group(1).strip()
        org = re.search(r"ORG[;:](.+)", text, re.IGNORECASE)
        if org:
            result["company"] = org.group(1).strip().rstrip(";")
        title = re.search(r"TITLE[;:](.+)", text, re.IGNORECASE)
        if title:
            result["title"] = title.group(1).strip()
        email = re.search(r"EMAIL[^:]*:(.+)", text, re.IGNORECASE)
        if email:
            result["email"] = email.group(1).strip()
        tel = re.search(r"TEL[^:]*:(.+)", text, re.IGNORECASE)
        if tel:
            result["phone"] = tel.group(1).strip()
        url = re.search(r"URL[;:](.+)", text, re.IGNORECASE)
        if url:
            result["url"] = url.group(1).strip()
        return result
    # Try JSON
    try:
        data = json.loads(text)
        result["format"] = "JSON"
        for key in ["name", "full_name", "fullName", "attendee_name"]:
            if key in data:
                result["name"] = str(data[key])
                break
        for key in ["title", "job_title", "jobTitle", "position"]:
            if key in data:
                result["title"] = str(data[key])
                break
        for key in ["company", "organization", "org", "company_name"]:
            if key in data:
                result["company"] = str(data[key])
                break
        for key in ["email", "email_address", "emailAddress"]:
            if key in data:
                result["email"] = str(data[key])
                break
        for key in ["phone", "telephone", "mobile", "phone_number"]:
            if key in data:
                result["phone"] = str(data[key])
                break
        return result
    except (json.JSONDecodeError, ValueError):
        pass
    # Try URL
    if text.startswith(("http://", "https://")):
        result["format"] = "URL"
        result["url"] = text
        return result
    # Try MECARD
    if text.startswith("MECARD:"):
        result["format"] = "MECARD"
        n = re.search(r"N:([^;]+)", text)
        if n:
            result["name"] = n.group(1).strip()
        org = re.search(r"ORG:([^;]+)", text)
        if org:
            result["company"] = org.group(1).strip()
        email = re.search(r"EMAIL:([^;]+)", text)
        if email:
            result["email"] = email.group(1).strip()
        tel = re.search(r"TEL:([^;]+)", text)
        if tel:
            result["phone"] = tel.group(1).strip()
        return result
    # Plain text - try to extract email/phone
    result["format"] = "TEXT"
    email_match = re.search(r"[\\w.+-]+@[\\w-]+\\.[\\w.]+", text)
    if email_match:
        result["email"] = email_match.group(0)
    phone_match = re.search(r"[\\+]?[\\d\\s\\-\\(\\)]{7,15}", text)
    if phone_match:
        result["phone"] = phone_match.group(0).strip()
    return result
';

-- Prepare image binary data for Snowflake Document AI processing.
-- Used by: PROCESS_BADGE stored procedure (legacy).
CREATE OR REPLACE FUNCTION EXTRACT_TEXT_DOCUMENT_AI(IMAGE_DATA BINARY)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('pillow','numpy')
HANDLER = 'extract_with_document_ai'
AS '
import json

def extract_with_document_ai(image_data):
    """
    Extract text using Snowflake Cortex AI Document AI.
    Prepares image data for Document AI processing.
    """
    try:
        if not image_data or len(image_data) == 0:
            return {
                "success": False,
                "error": "No image data provided",
                "text": "",
                "confidence": 0,
                "blocks": []
            }
        import base64
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        return {
            "success": True,
            "image_base64": image_b64,
            "ready_for_document_ai": True,
            "image_size_bytes": len(image_data)
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Image preparation failed: {str(e)}",
            "text": "",
            "confidence": 0
        }
';

-- ============================================================
-- 6. STORED PROCEDURES
-- ============================================================

-- Full badge processing pipeline (SQL-based).
-- Calls PROCESS_QR_CODE, EXTRACT_TEXT_OCR, PARSE_CONTACT_INFO UDFs
-- (external dependencies — may not exist in all deployments).
-- Inserts into BADGE_SCANS and PROCESSING_LOGS.
CREATE OR REPLACE PROCEDURE PROCESS_BADGE(
    IMAGE_FILENAME VARCHAR,
    IMAGE_DATA BINARY,
    CREATED_BY VARCHAR DEFAULT 'native_app_user'
)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS OWNER
AS '
DECLARE
    scan_id NUMBER;
    qr_result VARIANT;
    ocr_result VARIANT;
    contact_result VARIANT;
    stage_path STRING;
    processing_start TIMESTAMP_NTZ;
    processing_end TIMESTAMP_NTZ;
    execution_time NUMBER;
    final_result VARIANT;
BEGIN
    processing_start := CURRENT_TIMESTAMP();

    -- Generate stage path
    stage_path := CONCAT(''@BADGE_IMAGES_STAGE/'', image_filename);

    -- Process QR codes
    qr_result := PROCESS_QR_CODE(image_data);

    -- Extract text via OCR
    ocr_result := EXTRACT_TEXT_OCR(image_data);

    -- Parse contact information if text was extracted
    IF (ocr_result:text::STRING IS NOT NULL AND LENGTH(ocr_result:text::STRING) > 0) THEN
        contact_result := PARSE_CONTACT_INFO(ocr_result:text::STRING);
    ELSE
        contact_result := OBJECT_CONSTRUCT(
            ''name'', NULL,
            ''title'', NULL,
            ''company'', NULL,
            ''email'', NULL,
            ''phone'', NULL
        );
    END IF;

    -- Calculate overall confidence
    DECLARE
        qr_confidence NUMBER DEFAULT 0;
        ocr_confidence NUMBER DEFAULT 0;
        overall_confidence NUMBER;
    BEGIN
        qr_confidence := CASE WHEN ARRAY_SIZE(qr_result:qr_codes) > 0 THEN 100 ELSE 0 END;
        ocr_confidence := COALESCE(ocr_result:confidence::NUMBER, 0);
        overall_confidence := (qr_confidence + ocr_confidence) / 2;
    END;

    -- Insert badge scan record
    INSERT INTO BADGE_SCANS (
        IMAGE_FILENAME,
        IMAGE_STAGE_PATH,
        QR_CODE_DATA,
        EXTRACTED_TEXT,
        PARSED_NAME,
        PARSED_TITLE,
        PARSED_COMPANY,
        PARSED_EMAIL,
        PARSED_PHONE,
        CONFIDENCE_SCORE,
        PROCESSING_STATUS,
        CREATED_BY,
        METADATA
    ) VALUES (
        :image_filename,
        :stage_path,
        qr_result:qr_codes::STRING,
        ocr_result:text::STRING,
        contact_result:name::STRING,
        contact_result:title::STRING,
        contact_result:company::STRING,
        contact_result:email::STRING,
        contact_result:phone::STRING,
        :overall_confidence,
        ''COMPLETED'',
        :created_by,
        OBJECT_CONSTRUCT(
            ''qr_result'', qr_result,
            ''ocr_result'', ocr_result,
            ''contact_result'', contact_result,
            ''processing_timestamp'', processing_start
        )
    );

    scan_id := LAST_INSERT_ID();

    processing_end := CURRENT_TIMESTAMP();
    execution_time := DATEDIFF(millisecond, processing_start, processing_end);

    -- Log the processing operation
    INSERT INTO PROCESSING_LOGS (
        BADGE_SCAN_ID,
        OPERATION,
        STATUS,
        DETAILS,
        EXECUTION_TIME_MS
    ) VALUES (
        :scan_id,
        ''PROCESS_BADGE'',
        ''SUCCESS'',
        ''Badge processed successfully'',
        :execution_time
    );

    -- Return comprehensive result
    final_result := OBJECT_CONSTRUCT(
        ''scan_id'', scan_id,
        ''success'', TRUE,
        ''qr_codes'', qr_result:qr_codes,
        ''extracted_text'', ocr_result:text,
        ''contact_info'', contact_result,
        ''confidence_score'', overall_confidence,
        ''execution_time_ms'', execution_time
    );

    RETURN final_result;

EXCEPTION
    WHEN OTHER THEN
        INSERT INTO PROCESSING_LOGS (
            BADGE_SCAN_ID,
            OPERATION,
            STATUS,
            DETAILS,
            ERROR_MESSAGE,
            EXECUTION_TIME_MS
        ) VALUES (
            NULL,
            ''PROCESS_BADGE'',
            ''ERROR'',
            ''Badge processing failed'',
            SQLERRM,
            DATEDIFF(millisecond, processing_start, CURRENT_TIMESTAMP())
        );

        RETURN OBJECT_CONSTRUCT(
            ''success'', FALSE,
            ''error'', SQLERRM
        );
END;
';

-- Badge processing via Cortex AI (Python Snowpark-based).
-- Uses AI_EXTRACT for vision analysis and PARSE_QR_VCARD for QR parsing.
-- Inserts into BADGE_SCANS.
CREATE OR REPLACE PROCEDURE PROCESS_BADGE_IMAGE(
    P_FILENAME VARCHAR,
    P_QR_DATA VARCHAR DEFAULT NULL,
    P_CONFERENCE VARCHAR DEFAULT NULL,
    P_CREATED_BY VARCHAR DEFAULT NULL
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
EXECUTE AS OWNER
AS '
import json

def run(session, P_FILENAME, P_QR_DATA=None, P_CONFERENCE=None, P_CREATED_BY=None):
    v_extracted = {"name":None,"title":None,"company":None,"email":None,"phone":None}

    # Only attempt AI extraction if an image file was provided
    if P_FILENAME:
        try:
            result = session.sql(f"""
                SELECT AI_EXTRACT(
                    file => TO_FILE(''@DEMO.DEMO.BADGE_IMAGES_STAGE'', ''{P_FILENAME}''),
                    responseFormat => {{
                        ''name'': ''What is the full name of the person on this badge or name tag?'',
                        ''title'': ''What is the job title or role shown on this badge?'',
                        ''company'': ''What is the company or organization name on this badge?'',
                        ''email'': ''What is the email address shown on this badge? Return null if none.'',
                        ''phone'': ''What is the phone number shown on this badge? Return null if none.''
                    }}
                ) AS extracted
            """).collect()
            if result and result[0]["EXTRACTED"]:
                v_extracted = json.loads(result[0]["EXTRACTED"])
        except Exception as e:
            err_msg = str(e)[:500].replace(chr(10), chr(32)).replace(chr(13), chr(32))
            v_extracted["error"] = err_msg

    # Parse QR code data if provided
    v_qr = {"name":None,"title":None,"company":None,"email":None,"phone":None}
    if P_QR_DATA:
        try:
            safe_qr = P_QR_DATA.replace(chr(39), chr(39)+chr(39))
            qr_result = session.sql(f"SELECT DEMO.DEMO.PARSE_QR_VCARD(''{safe_qr}'') AS parsed").collect()
            if qr_result and qr_result[0]["PARSED"]:
                v_qr = json.loads(qr_result[0]["PARSED"])
        except Exception:
            pass

    # Merge: prefer AI-extracted, fall back to QR
    name = v_extracted.get("name") or v_qr.get("name")
    title = v_extracted.get("title") or v_qr.get("title")
    company = v_extracted.get("company") or v_qr.get("company")
    email = v_extracted.get("email") or v_qr.get("email")
    phone = v_extracted.get("phone") or v_qr.get("phone")
    confidence = 0.9 if name else 0.5
    stage_path = f"@DEMO.DEMO.BADGE_IMAGES_STAGE/{P_FILENAME}" if P_FILENAME else None

    def esc(v):
        if v is None:
            return "NULL"
        s = str(v)
        s = s.replace(chr(92), chr(92)+chr(92))
        s = s.replace(chr(10), chr(92)+chr(110))
        s = s.replace(chr(13), chr(92)+chr(114))
        s = s.replace(chr(39), chr(39)+chr(39))
        return chr(39) + s + chr(39)

    extracted_json = json.dumps(v_extracted)
    metadata_json = json.dumps({"ai_extracted": v_extracted, "qr_parsed": v_qr})

    session.sql(f"""
        INSERT INTO DEMO.DEMO.BADGE_SCANS (
            IMAGE_FILENAME, IMAGE_STAGE_PATH, QR_CODE_DATA, EXTRACTED_TEXT,
            PARSED_NAME, PARSED_TITLE, PARSED_COMPANY, PARSED_EMAIL, PARSED_PHONE,
            CONFIDENCE_SCORE, PROCESSING_STATUS, CREATED_BY, CONFERENCE_NAME,
            METADATA
        )
        SELECT
            {esc(P_FILENAME)}, {esc(stage_path)}, {esc(P_QR_DATA)},
            $${extracted_json}$$,
            {esc(name)}, {esc(title)}, {esc(company)}, {esc(email)}, {esc(phone)},
            {confidence}, ''PROCESSED'', {esc(P_CREATED_BY)}, {esc(P_CONFERENCE)},
            PARSE_JSON($${metadata_json}$$)
    """).collect()

    scan_id = session.sql("SELECT MAX(ID) AS MID FROM DEMO.DEMO.BADGE_SCANS").collect()[0]["MID"]

    return {
        "scan_id": scan_id,
        "name": name,
        "title": title,
        "company": company,
        "email": email,
        "phone": phone,
        "status": "PROCESSED"
    }
';

-- ============================================================
-- Done. All badge scanner objects are ready.
-- ============================================================
