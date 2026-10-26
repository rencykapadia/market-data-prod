import os
import re
import shutil
import pandas as pd
from azure.storage.blob import BlobServiceClient
import pyodbc
import logging
import sys
import json
from app.config.settings import  BLOB_CONNECTION_STRING, table_name, container_name
from app.services.sql_connection import connect_with_retry


# =========================
# LOGGER SETUP
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

# =========================
# PATHS
# =========================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

csv_input_folder  = os.path.join(BASE_DIR,  "output", "pending", "csv")
json_input_folder = os.path.join(BASE_DIR,  "output", "pending", "json")
processed_folder  = os.path.join(BASE_DIR,  "output", "processed")
failed_folder     = os.path.join(BASE_DIR,  "output", "failed")

for path in [csv_input_folder, json_input_folder, processed_folder, failed_folder]:
    os.makedirs(path, exist_ok=True)

# =========================
# COMPANY → LOCATION MAP
# Single source of truth.
# Add new companies here — nothing else needs to change.
# =========================
location = "New Mexico"

# =========================
# GLOBAL CONNECTION OBJECTS
# =========================
conn   = None
cursor = None

# =========================
# HELPER FUNCTIONS
# =========================
def to_sql_float(val):
    try:
        if val is None:
            return None
        s = str(val).strip()
        if s.lower() in ('nan', 'none', 'null', ''):
            return None
        cleaned = re.sub(r'[^\d.\-]', '', s)
        return float(cleaned) if cleaned else None
    except:
        return None


def to_sql_str(val):
    try:
        if val is None:
            return None
        s = str(val).strip()
        return s if s.lower() not in ('nan', 'none', 'null', '') else None
    except:
        return None


# =========================
# MAIN PIPELINE
# =========================
try:

    # ── SQL CONNECTION ──
    try:
        conn = connect_with_retry()
        cursor = conn.cursor()

        # ── ENSURE TABLE EXISTS ──
        try:
            logger.info("Ensuring table exists...")
            cursor.execute(f"""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='market_data' AND xtype='U')
                CREATE TABLE {table_name} (
                    date NVARCHAR(50),
                    location NVARCHAR(100),
                    company NVARCHAR(255),
                    dispensary_location NVARCHAR(255),
                    name NVARCHAR(255),
                    category NVARCHAR(100),
                    subcategory NVARCHAR(100),
                    brand NVARCHAR(100),
                    strain_type NVARCHAR(50),
                    THC NVARCHAR(50),
                    CBD NVARCHAR(50),
                    weight NVARCHAR(50),
                    original_price FLOAT,
                    deal_1 NVARCHAR(255),
                    deal_2 NVARCHAR(255),
                    deal_3 NVARCHAR(255),
                    deal_4 NVARCHAR(255),
                    deal_5 NVARCHAR(255),
                    deal_6 NVARCHAR(255),
                    deal_7 NVARCHAR(255),
                    deal_8 NVARCHAR(255),
                    deal_9 NVARCHAR(255),
                    deal_10 NVARCHAR(255)
                )
            """)
            conn.commit()
            logger.info("Table ready")
        except Exception as e:
            logger.critical(f"Table creation failed: {e}", exc_info=True)
            sys.exit(1)

    except Exception as e:
        logger.critical(f"SQL connection failed: {e}", exc_info=True)
        sys.exit(1)

    # ── BLOB CONNECTION ──
    try:
        logger.info("Connecting to Azure Blob...")
        blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
        logger.info("Blob connected")
    except Exception as e:
        logger.critical(f"Blob connection failed: {e}", exc_info=True)
        sys.exit(1)

    # =========================
    # JSON VALIDATION + UPLOAD
    # =========================
    json_files = [f for f in os.listdir(json_input_folder) if f.endswith(".json")]

    if not json_files:
        raise Exception("No JSON files found")

    logger.info(f"JSON files found: {len(json_files)}")

    for file in json_files:
        file_path = os.path.join(json_input_folder, file)

        try:
            if os.path.getsize(file_path) == 0:
                raise Exception(f"JSON file is empty: {file}")

            with open(file_path, "r", encoding="utf-8") as f:
                content = json.load(f)

            if not content or (isinstance(content, list) and len(content) == 0):
                raise Exception(f"JSON has no usable data: {file}")

            # filename format: <date>_<company>.json
            file_date, file_company = file.replace(".json", "").split("_", 1)

            # ── Dynamic location lookup ──
            #location  = get_location(file_company)
            blob_path = f"{location}/{file_company}/{file}"

            logger.info(f"Uploading JSON → {blob_path}")

            blob_client = blob_service_client.get_blob_client(
                container=container_name,
                blob=blob_path
            )

            with open(file_path, "rb") as data:
                blob_client.upload_blob(data, overwrite=True, timeout=300)

            shutil.move(file_path, os.path.join(processed_folder, file))

        except Exception as e:
            logger.critical(f"JSON upload failed: {file} → {e}", exc_info=True)
            shutil.move(file_path, os.path.join(failed_folder, file))
            raise

    # =========================
    # CSV PROCESSING + SQL INSERT
    # =========================
    csv_files = [f for f in os.listdir(csv_input_folder) if f.endswith(".csv")]

    if not csv_files:
        raise Exception("No CSV files found")

    logger.info(f"CSV files found: {len(csv_files)}")

    for file in csv_files:
        file_path = os.path.join(csv_input_folder, file)

        try:
            logger.info(f"Processing CSV: {file}")

            file_date, file_company = file.replace(".csv", "").split("_", 1)

            # ── Dynamic location lookup ──
            #location = get_location(file_company)

            try:
                df = pd.read_csv(file_path, dtype=object, encoding="utf-8")
            except Exception:
                try:
                    df = pd.read_csv(file_path, dtype=object, encoding="utf-16")
                except Exception:
                    df = pd.read_csv(file_path, dtype=object, encoding="latin1")

            if df.empty or df.shape[1] == 0:
                raise Exception(f"CSV has no usable data: {file}")

            logger.info(f"Rows: {len(df)} | Company: {file_company} | Location: {location}")

            for i in range(1, 11):
                col = f"deal_{i}"
                if col not in df.columns:
                    df[col] = None

            # Idempotency — delete existing rows for same date+company
            cursor.execute(
                f"SELECT COUNT(1) FROM {table_name} WHERE date=? AND company=?",
                file_date, file_company
            )
            if cursor.fetchone()[0] > 0:
                logger.info("Deleting existing records...")
                cursor.execute(
                    f"DELETE FROM {table_name} WHERE date=? AND company=?",
                    file_date, file_company
                )
                conn.commit()

            rows = []
            for _, row in df.iterrows():
                rows.append((
                    to_sql_str(row.get("date")),
                    to_sql_str(row.get("location") or "New Mexico"),  # CSV location ("New Mexico") — the
                    # export filters on it (WHERE location='New Mexico'); do NOT store the "NM" blob-path code.
                    to_sql_str(row.get("company")),
                    to_sql_str(row.get("dispensary_location")),
                    to_sql_str(row.get("name")),
                    to_sql_str(row.get("category")),
                    to_sql_str(row.get("subcategory")),
                    to_sql_str(row.get("brand")),
                    to_sql_str(row.get("strain_type")),
                    to_sql_str(row.get("THC")),
                    to_sql_str(row.get("CBD")),
                    to_sql_str(row.get("weight")),
                    to_sql_float(row.get("original_price")),
                    *[to_sql_str(row.get(f"deal_{i}")) for i in range(1, 11)]
                ))

            cursor.fast_executemany = True
            cursor.executemany(f"""
                INSERT INTO {table_name} (
                    date, location, company, dispensary_location,
                    name, category, subcategory,
                    brand, strain_type, THC, CBD,
                    weight, original_price,
                    deal_1, deal_2, deal_3, deal_4, deal_5,
                    deal_6, deal_7, deal_8, deal_9, deal_10
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)

            conn.commit()
            logger.info(f"Inserted {len(rows)} rows")

            shutil.move(file_path, os.path.join(processed_folder, file))

        except Exception as e:
            logger.critical(f"CSV processing failed: {file} → {e}", exc_info=True)
            shutil.move(file_path, os.path.join(failed_folder, file))
            raise

except Exception as e:
    logger.critical(f"PIPELINE FAILED: {e}", exc_info=True)
    sys.exit(1)

finally:
    try:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        logger.info("SQL connection closed")
    except Exception as e:
        logger.error(f"Error closing SQL connection: {e}")