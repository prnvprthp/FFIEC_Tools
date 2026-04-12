import os
import time
import glob
import zipfile
import shutil
import json
import csv
from datetime import datetime
import xml.etree.ElementTree as ET
import mysql.connector
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- Database Configuration ---
DB_HOST     = "localhost" 
DB_PORT     = 3306
DB_USER     = "root"
DB_PASSWORD = "root@123"
DB_NAME     = "ffiec_data"
DB_TABLE    = "call_reports"

CHECKPOINT_FILE = "parsed_folders_local.json"

# Global variables for worker processes
worker_conn = None
worker_cursor = None

def init_worker():
    """Give every CPU core its own dedicated database connection."""
    global worker_conn, worker_cursor
    worker_conn = mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, 
        password=DB_PASSWORD, database=DB_NAME, 
        autocommit=False # MASSIVE SPEED BOOST
    )
    worker_cursor = worker_conn.cursor()

def setup_database():
    """Creates database and main table if they do not exist."""
    engine = create_engine(f"mysql+mysqlconnector://{DB_USER}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}")
    with engine.begin() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}"))
        
    engine = create_engine(f"mysql+mysqlconnector://{DB_USER}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    with engine.begin() as conn:
        create_table_query = f"""
        CREATE TABLE IF NOT EXISTS {DB_TABLE} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            idrssd INT,
            bank_name VARCHAR(255),
            source_folder VARCHAR(50),
            concept_reference VARCHAR(100),
            value TEXT,
            unit_ref VARCHAR(50),
            context_ref VARCHAR(100)
        ) ENGINE=InnoDB;
        """
        conn.execute(text(create_table_query))

def load_checkpoint():
    """Loads JSON tracking from the ROOT directory so it isn't deleted by Streamlit temp cleanup."""
    cwd = os.getcwd() 
    path = os.path.join(cwd, CHECKPOINT_FILE)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {"parsed_folders": {}}

def save_checkpoint(data):
    """Saves JSON tracking to the ROOT directory."""
    cwd = os.getcwd()
    path = os.path.join(cwd, CHECKPOINT_FILE)
    with open(path, 'w') as f:
        json.dump(data, f, indent=4)

def get_date_objects(date_str):
    """Safely extracts and parses the date, returning None if invalid."""
    try:
        # Grabs just the first 10 characters to ensure it only reads MM/DD/YYYY
        # even if FFIEC adds trailing spaces or hidden characters
        date_part = date_str.strip()[:10]
        return datetime.strptime(date_part, "%m/%d/%Y")
    except ValueError:
        return None

def load_bank_lookup(por_path):
    """Loads RSSD to Bank Name mapping from the POR metadata file (V4 logic)."""
    bank_lookup = {}
    try:
        with open(por_path, 'r', encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                rssd = row.get("IDRSSD", "").strip()
                name = row.get("Financial Institution Name", "").strip()
                if rssd and name:
                    try:
                        bank_lookup[int(rssd)] = name
                    except ValueError:
                        pass
    except Exception:
        pass
    return bank_lookup

def process_file_and_insert(args):
    """Worker function for multiprocessing XML parsing (Namespace Agnostic)."""
    filepath, bank_lookup, source_folder = args
    global worker_conn, worker_cursor
    rows_to_insert = []
    
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        
        # 1. Namespace-Agnostic ID finder
        idrssd = None
        for elem in root.iter():
            if elem.tag.endswith('identifier'):
                if elem.text:
                    try:
                        idrssd = int(elem.text)
                        break
                    except ValueError:
                        pass

        if not idrssd:
            return 0 

        bank_name = bank_lookup.get(idrssd, "Unknown")

        # 2. Namespace-Agnostic Concept finder
        for child in root:
            if 'contextRef' in child.attrib:
                concept_ref = child.tag.split('}')[-1]
                value = child.text.strip() if child.text else None
                unit_ref = child.attrib.get('unitRef')
                context_ref = child.attrib.get('contextRef')

                if value is not None:
                    rows_to_insert.append((
                        idrssd, bank_name, source_folder, concept_ref, value, unit_ref, context_ref
                    ))

        if rows_to_insert:
            query = f"""
                INSERT INTO {DB_TABLE} 
                (idrssd, bank_name, source_folder, concept_reference, value, unit_ref, context_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            worker_cursor.executemany(query, rows_to_insert)
            worker_conn.commit()
            
        return len(rows_to_insert)

    except Exception:
        if worker_conn: worker_conn.rollback()
        return 0
# ==========================================
# GENERATOR 1: DOWNLOADER (SMART & RANGE MODES)
# ==========================================
def run_bulk_download(start_date_str, end_date_str, download_dir, mode="range"):
    yield ("Step 1: Configuring visible browser...", 0.0)
    
    chrome_options = Options()
    # chrome_options.add_argument("--headless=new") # Uncomment to run invisibly once tested!
    chrome_options.add_argument("--window-size=1920,1080")
    
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "directory_upgrade": True,
        "profile.default_content_setting_values.automatic_downloads": 1 
    }
    chrome_options.add_experimental_option("prefs", prefs)
    
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 30)

    try:
        yield ("Step 2: Accessing FFIEC Bulk Download Page...", 0.05)
        driver.get("https://cdr.ffiec.gov/public/pws/downloadbulkdata.aspx")
        time.sleep(2)
        
        yield ("Step 3: Selecting 'Call Reports -- Single Period'...", 0.05)
        try:
            product_dropdown = wait.until(EC.presence_of_element_located((By.ID, "ListBox1")))
            Select(product_dropdown).select_by_visible_text("Call Reports -- Single Period")
        except Exception:
            product_dropdown = wait.until(EC.presence_of_element_located((By.XPATH, "//select[.//option[contains(text(), 'Call Reports -- Single Period')]]")))
            Select(product_dropdown).select_by_visible_text("Call Reports -- Single Period")
            
        time.sleep(2) 
        
        yield ("Step 4: Selecting XBRL to trigger date population...", 0.05)
        xbrl_radio = wait.until(EC.element_to_be_clickable((By.ID, "XBRLRadiobutton")))
        driver.execute_script("arguments[0].click();", xbrl_radio)
        time.sleep(2) 
        
        yield ("Step 5: Waiting for FFIEC server to populate dates...", 0.05)
        date_dropdown_el = wait.until(EC.presence_of_element_located((By.ID, "DatesDropDownList")))
        wait.until(lambda d: len(Select(date_dropdown_el).options) > 1) 
        
        date_select = Select(date_dropdown_el)
        all_options = [opt.text.strip() for opt in date_select.options if opt.text.strip()]
        
        # --- NEW SMART LOGIC HERE ---
        target_dates = []
        
        if mode == "range":
            yield (f"Step 6: Filtering {len(all_options)} dropdown dates by requested range...", 0.05)
            start_dt = datetime.strptime(start_date_str, "%m/%d/%Y")
            end_dt = datetime.strptime(end_date_str, "%m/%d/%Y")
            for opt in all_options:
                opt_dt = get_date_objects(opt)
                if opt_dt and (start_dt <= opt_dt <= end_dt):
                    target_dates.append(opt)
                    
        elif mode == "smart":
            yield (f"Step 6: Cross-referencing {len(all_options)} dropdown dates with local database...", 0.05)
            # Load the tracking JSON file
            checkpoint = load_checkpoint()
            parsed_keys = checkpoint.get("parsed_folders", {}).keys()
            
            for opt in all_options:
                opt_dt = get_date_objects(opt)
                if not opt_dt:
                    continue
                
                # FFIEC Zips are named using MMDDYYYY (e.g. 12/31/2001 becomes 12312001)
                date_str_compact = opt_dt.strftime("%m%d%Y")
                
                # If this date string is NOT found in any of the previously parsed ZIP file names, we need it!
                already_parsed = any(date_str_compact in key for key in parsed_keys)
                if not already_parsed:
                    target_dates.append(opt)
        
        total_files = len(target_dates)
        
        if total_files == 0:
            yield (f"All caught up! Found 0 new/missing dates to download.", 1.0)
            time.sleep(3)
            return

        yield (f"Step 7: Successfully matched {total_files} files to download. Starting loop...", 0.1)

        for idx, target in enumerate(target_dates):
            current_progress = (idx / total_files) * 0.9 + 0.1
            yield (f"Requesting download for {target}...", current_progress)
            
            date_el = wait.until(EC.presence_of_element_located((By.ID, "DatesDropDownList")))
            Select(date_el).select_by_visible_text(target)
            time.sleep(1)

            download_btn = wait.until(EC.element_to_be_clickable((By.ID, "Download_0")))
            driver.execute_script("arguments[0].click();", download_btn)
            
            yield (f"Waiting up to 60s for FFIEC server to build ZIP for {target}...", current_progress)
            
            timeout = 60
            elapsed = 0
            file_started = False
            
            while elapsed < timeout:
                if glob.glob(os.path.join(download_dir, "*.crdownload")) or glob.glob(os.path.join(download_dir, "*.zip")):
                    file_started = True
                    break
                time.sleep(2)
                elapsed += 2
                
            if not file_started:
                yield (f"WARNING: File {target} never started downloading! Skipping...", current_progress)
                continue
            
            yield (f"File generation complete! Downloading {target} to disk...", current_progress)
            while glob.glob(os.path.join(download_dir, "*.crdownload")):
                time.sleep(2)
                
        yield ("Step 8: All requested ZIP files downloaded successfully.", 1.0)
        time.sleep(3)
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(error_details) 
        yield (f"CRITICAL ERROR in Downloader: {str(e)}", 1.0)
        time.sleep(10) 
    finally:
        driver.quit()

# ==========================================
# GENERATOR 2: PARSER
# ==========================================
def run_bulk_parse(download_dir):
    yield ("Scanning for downloaded ZIP files...", 0.0)
    
    zip_files = sorted(glob.glob(os.path.join(download_dir, "*.zip")))
    total_zips = len(zip_files)
    
    if total_zips == 0:
        yield ("No ZIP files found to parse.", 1.0)
        return

    yield ("Verifying database and table structure...", 0.0)
    setup_database()

    checkpoint = load_checkpoint()
    cpu_cores = max(1, multiprocessing.cpu_count() - 1)

    # Added start=1 so the counter reads (1/5) instead of (0/5)
    for zip_idx, zip_path in enumerate(zip_files, start=1):
        zip_name = os.path.basename(zip_path).replace('.zip', '')
        base_progress = (zip_idx - 1) / total_zips
        
        if zip_name in checkpoint.get("parsed_folders", {}):
            yield (f"({zip_idx}/{total_zips}) Skipping {zip_name}, already marked as parsed in JSON.", base_progress)
            continue
        
        yield (f"({zip_idx}/{total_zips}) Extracting {zip_name}...", base_progress)
        
        extract_to = os.path.join(download_dir, f"temp_{zip_name}")
        os.makedirs(extract_to, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_to)
            
        txt_files = glob.glob(os.path.join(extract_to, "**", "*.txt"), recursive=True)
        por_path = next((p for p in txt_files if "POR" in os.path.basename(p).upper()), None)
        
        if not por_path:
            yield (f"({zip_idx}/{total_zips}) No POR file found in {zip_name}. Skipping.", base_progress)
            shutil.rmtree(extract_to, ignore_errors=True)
            continue
            
        bank_lookup = load_bank_lookup(por_path)

        xml_files = sorted(glob.glob(os.path.join(extract_to, "**", "*.xml"), recursive=True))
        total_xmls = len(xml_files)
        
        if total_xmls == 0:
            shutil.rmtree(extract_to, ignore_errors=True)
            continue
            
        yield (f"({zip_idx}/{total_zips}) Found {total_xmls} XMLs. Starting multicore SQL push...", base_progress)

        task_args = [(path, bank_lookup, zip_name) for path in xml_files]
        total_rows = 0
        
        with ProcessPoolExecutor(max_workers=cpu_cores, initializer=init_worker) as executor:
            for i, rows_inserted in enumerate(executor.map(process_file_and_insert, task_args, chunksize=25)):
                total_rows += rows_inserted
                # Updates the frontend every 50 XML files with BOTH the ZIP counter and the XML counter
                if i % 50 == 0:
                    xml_progress = (i / total_xmls) * (1 / total_zips)
                    yield (f"({zip_idx}/{total_zips}) Pushing {zip_name}: XML File {i}/{total_xmls}...", base_progress + xml_progress)

        if "parsed_folders" not in checkpoint:
            checkpoint["parsed_folders"] = {}
        checkpoint["parsed_folders"][zip_name] = {"records": total_rows, "parsed_at": str(datetime.now())}
        save_checkpoint(checkpoint)

        shutil.rmtree(extract_to, ignore_errors=True)

    yield ("All files successfully parsed and pushed to Database.", 1.0)