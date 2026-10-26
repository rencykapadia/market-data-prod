import glob
import logging
import os
import subprocess
import sys
import time



# PATHS
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.environ["PYTHONPATH"] = BASE_DIR

SCRIPTS_DIR  = os.path.join(BASE_DIR,"app", "scripts")
SERVICES_DIR = os.path.join(BASE_DIR,"app", "services")

FAILED_FILE = os.path.join(SCRIPTS_DIR, "failed_scrapers.txt")
print(f"Failed file path: {FAILED_FILE}")

# 2. NOW IT IS SAFE TO REMOVE IT
# if os.path.exists(FAILED_FILE):
#     try:
#         os.remove(FAILED_FILE)
#         print("Old failed_scrapers.txt removed successfully.")
#     except Exception as e:
#         print(f"Could not remove old file: {e}")

# Remove old failed file if exists
# if os.path.exists(FAILED_FILE):
#     os.remove(FAILED_FILE)


# LOGGER
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)


def run_script(script_path):
    """
    Executes a Python script with real-time logging.
    Returns:
        success (bool), error_output (str)
    """
    try:
        logger.info(f"Starting: {script_path}")
 
        process = subprocess.Popen(
            [sys.executable, "-u", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
 
        logs = []
 
        for line in process.stdout:
            print(line, end="")
            logs.append(line)
 
        process.wait()
 
        if process.returncode != 0:
            logger.error(f"FAILED: {script_path}")
            return False, "".join(logs)
 
        logger.info(f"Completed: {script_path}")
        return True, ""
 
    except Exception as e:
        logger.error(f"Error running {script_path}: {e}")
        return False, str(e)


def main():
    logger.info("PIPELINE STARTED")

    # Auto-discover all .py scripts in each region folder.
    # Adding a new scraper file = picked up automatically, no change needed here.
    # MA_scripts      = sorted(glob.glob(os.path.join(SCRIPTS_DIR, "MA",     "*.py")))
    # tuscon_scripts      = sorted(glob.glob(os.path.join(SCRIPTS_DIR, "tuscon",     "*.py")))
    # Connecticut_scripts      = sorted(glob.glob(os.path.join(SCRIPTS_DIR, "conneticut",     "*.py")))
    # Pennsylvania_scripts      = sorted(glob.glob(os.path.join(SCRIPTS_DIR, "Pennsylvania",     "*.py")))
    New_Mexico_scripts = sorted(glob.glob(os.path.join(SCRIPTS_DIR, "NM",     "*.py")))

    scraper_scripts =  New_Mexico_scripts

    if not scraper_scripts:
        logger.error(f"No scraper scripts found under {SCRIPTS_DIR}")
        sys.exit(1)

    logger.info(f"Found {len(scraper_scripts)} scraper(s): "
                f"{[os.path.basename(s) for s in scraper_scripts]}")

    # =========================
    # STEP 1: RUN SCRAPERS
    # =========================
    for script in scraper_scripts:
        success, error = run_script(script)

        if not success:
            script_name = os.path.basename(script)

            logger.error(f"Scraper failed: {script_name}")

            # Append failed scraper name to file
            with open(FAILED_FILE, "a", encoding="utf-8") as f:
                f.write(f"{script_name}\n")

            if error:
                logger.error("----- ERROR LOGS -----")
                logger.error(error[:2000])

            # Continue remaining scrapers
            continue

        time.sleep(5)

    # # =========================
    # # STEP 2: RUN BLOB + SQL
    # # =========================
    success, error = run_script(os.path.join(SERVICES_DIR, "upload_to_blob_and_sql_pipeline.py"))

    if not success:
        logger.error("Upload/SQL pipeline failed")

        if error:
            logger.error("----- ERROR LOGS -----")
            logger.error(error[:2000])

        sys.exit(1)

    # =========================
    # STEP 3: EXPORT + EMAIL
    # =========================
    success, error = run_script(os.path.join(SERVICES_DIR, "export_and_mail.py"))

    if not success:
        logger.error("Export/Email step failed")

        if error:
            logger.error("----- ERROR LOGS -----")
            logger.error(error[:2000])

        sys.exit(1)

    # =========================
    # SUCCESS
    # =========================
    logger.info("PIPELINE SUCCESS")
    sys.exit(0)


if __name__ == "__main__":
    main()