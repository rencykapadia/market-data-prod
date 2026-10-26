import os
import sys
import logging
import pandas as pd
import pyodbc
import smtplib
from email.message import EmailMessage
from datetime import datetime
import pytz
from app.config.settings import MAIL_SERVICE_PASSWORD, table_name, view_name, EMAIL_FROM, recipients
from app.services.sql_connection import connect_with_retry

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")


# =========================
# LOGGER
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)


MAIL_FOLDER = "app/output/mail"

FAILED_FILE = os.path.join(
    SCRIPTS_DIR,
    "failed_scrapers.txt"
)
print(f"Failed file path: {FAILED_FILE}")


# =========================
# DATE
# =========================
def get_target_date():
    us_tz = pytz.timezone("America/Phoenix")
    return datetime.now(us_tz).strftime("%Y-%m-%d")


# =========================
# MAIN
# =========================
conn = None
file_path = None   # ✅ FIX: avoid reference before assignment

try:

    logger.info("EXPORT + EMAIL STARTED")

    # DATE
    try:
        date = get_target_date()
        logger.info(f"Using date: {date}")

    except Exception as e:
        raise Exception(f"Date step failed: {e}")

    # SQL CONNECTION
    conn = connect_with_retry()
    cursor = conn.cursor()

    # FETCH DATA (TABLE)
    try:

        logger.info("Fetching data...")

        query = f"SELECT * FROM {table_name} WHERE date = ?  AND location='New Mexico'"

        df = pd.read_sql(query, conn, params=[date])

        if df.empty:
            raise Exception(f"No data found for {date} date")

        logger.info(f"Fetched {len(df)} rows")

    except Exception as e:
        raise Exception(f"Fetch step failed: {e}")

    # FETCH DATA (VIEW)
    try:

        logger.info("Fetching view data...")

        view_query = f"""
        SELECT *
        FROM {view_name}
        WHERE date = ?
        AND deal_1 IS NOT NULL AND location='New Mexico'
        """

        view_df = pd.read_sql(view_query, conn, params=[date])

        if view_df.empty:
            raise Exception(f"No data found in view for {date} date")

        logger.info(f"View fetched {len(view_df)} rows")

    except Exception as e:
        raise Exception(f"View fetch failed: {e}")

    # EXPORT EXCEL (2 SHEETS)
    try:

        os.makedirs(MAIL_FOLDER, exist_ok=True)

        file_path = os.path.join(
            MAIL_FOLDER,
            f"market_data_New_Mexico_{date}.xlsx"
        )

        with pd.ExcelWriter(file_path, engine="openpyxl") as writer:

            df.to_excel(
                writer,
                sheet_name="market_data",
                index=False
            )

            view_df.to_excel(
                writer,
                sheet_name="only_deals_data",
                index=False
            )

        if not os.path.exists(file_path):
            raise Exception("Excel file not created")

        logger.info(f"Excel created: {file_path}")

    except Exception as e:
        raise Exception(f"Excel export failed: {e}")

    # SEND EMAIL
    try:

        if not all([EMAIL_FROM, MAIL_SERVICE_PASSWORD, recipients]):
            raise Exception("Missing email credentials")

        if not file_path or not os.path.exists(file_path):
            raise Exception("Excel file missing, cannot send email")

        msg = EmailMessage()

        msg["Subject"] = f"Market Data Report - {date} (New Mexico)"
        msg["From"] = EMAIL_FROM
        msg["To"] = ", ".join(recipients)

        # =========================
        # EMAIL BODY
        # =========================
        body = f"Hi all,\n\nPlease find attached market data report for {date}."
        
        if os.path.exists(FAILED_FILE):
            with open(FAILED_FILE, "r", encoding="utf-8") as f:
                if f.read().strip():
                    body += (
                        "\n\n⚠ Some scrapers failed during execution."
                        "\nPlease check attached failed_scrapers.txt"
                        "\nData for those scrapers may be missing."
                    )

        body += "\n\nThanks"

        msg.set_content(body)

        # =========================
        # ATTACH EXCEL
        # =========================
        with open(file_path, "rb") as f:

            msg.add_attachment(
                f.read(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=os.path.basename(file_path)
            )

        # =========================
        # ATTACH FAILED FILE
        # =========================
        if os.path.exists(FAILED_FILE):

            with open(FAILED_FILE, "rb") as f:

                msg.add_attachment(
                    f.read(),
                    maintype="text",
                    subtype="plain",
                    filename=os.path.basename(FAILED_FILE)
                )

            logger.warning("Attached failed_scrapers.txt")

        logger.info("Sending email...")

        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:

            smtp.starttls()

            smtp.login(
                EMAIL_FROM,
                MAIL_SERVICE_PASSWORD
            )

            smtp.send_message(msg)

        logger.info("Email sent successfully")

        # =========================
        # DELETE FAILED FILE
        # =========================
        if os.path.exists(FAILED_FILE):
            with open(FAILED_FILE, "w") as f:
                pass  # Opening in 'w' mode automatically clears the file
            
            logger.info("Cleared the contents of failed_scrapers.txt")


    except Exception as e:
        raise Exception(f"Email step failed: {e}")

    logger.info("EXPORT + EMAIL SUCCESS")

except Exception as e:

    logger.critical(
        f"EXPORT + EMAIL FAILED: {e}",
        exc_info=True
    )

    sys.exit(1)

finally:

    try:

        if conn:
            conn.close()
            logger.info("SQL connection closed")

    except Exception as e:
        logger.error(f"Error closing SQL connection: {e}")