import pyodbc
import logging
import time

from app.config.settings import (
    SQL_PASSWORD,
    server,
    database,
    username
)

# =========================
# LOGGER
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


def connect_with_retry(max_retries=5, delay=10):
    """
    Connect to SQL Server with retry logic.
    """

    connection_string = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={SQL_PASSWORD};"
    )

    for attempt in range(1, max_retries + 1):

        try:
            logger.info(f"Connecting to SQL (Attempt {attempt}/{max_retries})...")

            conn = pyodbc.connect(connection_string)

            # Test connection
            conn.cursor().execute("SELECT 1")

            logger.info("SQL connected successfully")

            return conn

        except Exception as e:

            logger.warning(f"Connection attempt failed: {e}")

            if attempt < max_retries:
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)

            else:
                logger.critical("Max retries reached. SQL connection failed.")
                raise