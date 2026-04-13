import os
import time
import glob
import zipfile
import shutil
import json
import csv
import re
from datetime import datetime
import xml.etree.ElementTree as ET
from sqlalchemy import create_engine, text
import streamlit as st

# --- Database Configuration ---
DB_NAME             = "ffiec_data"
TABLE_FINANCIALS    = "call_reports_financials"
TABLE_POR           = "call_reports_por"
TABLE_LOG           = "migration_log"

def get_db_engine(url):
    """Creates a SQLAlchemy engine with bulletproof TiDB/MySQL URL parsing."""
    # Aggressively standardize the connection scheme to prevent SQLAlchemy unpacking errors
    if "://" in url:
        scheme, rest = url.split("://", 1)
        # If it's any variation of MySQL/TiDB, force it to be exactly mysql+pymysql
        if "mysql" in scheme or "tidb" in scheme:
            url = f"mysql+pymysql://{rest}"
            
    # Apply SSL arguments for TiDB Cloud
    connect_args = {"ssl": {"fake_config": True}} if "tidbcloud.com" in url else {}
    
    return create_engine(url, connect_args=connect_args)

def setup_database():
    """Creates database tables if they do not exist and fixes schema mismatches."""
    DB_URL = st.secrets["DB_URL"]
    engine = get_db_engine(DB_URL)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {TABLE_FINANCIALS} (id INT AUTO_INCREMENT PRIMARY KEY, idrssd INT, report_date VARCHAR(50), concept_reference VARCHAR(100), value TEXT, unit_ref VARCHAR(50), context_ref VARCHAR(100)) ENGINE=InnoDB;"))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {TABLE_POR} (idrssd INT PRIMARY KEY, bank_name VARCHAR(255)) ENGINE=InnoDB;"))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {TABLE_LOG} (report_date VARCHAR(50) PRIMARY KEY, status VARCHAR(20), records_inserted INT, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP) ENGINE=InnoDB;"))
        try: conn.execute(text(f"ALTER TABLE {TABLE_POR} DROP COLUMN source_folder;"))
        except Exception: pass 
        try:
            conn.execute(text(f"CREATE INDEX idx_report_date ON {TABLE_FINANCIALS}(report_date);"))
            conn.execute(text(f"CREATE INDEX idx_idrssd ON {TABLE_FINANCIALS}(idrssd);"))
        except Exception: pass 

def load_checkpoint():
    """Queries the Migration Log to see which periods are TRULY completed."""
    try:
        DB_URL = st.secrets["DB_URL"]
        engine = get_db_engine(DB_URL)
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT report_date FROM {TABLE_LOG} WHERE status = 'COMPLETED'")).fetchall()
            return {"parsed_folders": {row[0]: True for row in result}}
    except Exception: pass 
    return {"parsed_folders": {}}

def deduplicate_data():
    """Removes exact duplicate rows using a high-speed, memory-efficient chunking strategy."""
    DB_URL = st.secrets["DB_URL"]
    engine = get_db_engine(DB_URL)
    total_removed = 0
    with engine.connect() as outer_conn:
        dates = outer_conn.execute(text(f"SELECT DISTINCT report_date FROM {TABLE_FINANCIALS}")).fetchall()
        report_dates = [d[0] for d in dates]
    if not report_dates:
        yield ("No data found to deduplicate.", 1.0, 0)
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMPORARY TABLE temp_keep_ids (id INT PRIMARY KEY);"))
    total_periods = len(report_dates)
    for period_idx, rd in enumerate(report_dates):
        with engine.connect() as conn:
            rssd_results = conn.execute(text(f"SELECT DISTINCT idrssd FROM {TABLE_FINANCIALS} WHERE report_date = :rd"), {"rd": rd}).fetchall()
            rssds = [r[0] for r in rssd_results]
        if not rssds: continue
        chunk_size = 1000 
        total_chunks = (len(rssds) + chunk_size - 1) // chunk_size
        for chunk_idx, i in enumerate(range(0, len(rssds), chunk_size)):
            rssd_chunk = rssds[i:i + chunk_size]
            rssds_str = ",".join(map(str, rssd_chunk))
            overall_progress = (period_idx / total_periods) + (chunk_idx / total_chunks / total_periods)
            yield (f"Cleaning {rd}: Batch {chunk_idx+1}/{total_chunks}...", overall_progress, total_removed)
            with engine.begin() as conn:
                query_insert = text(f"INSERT IGNORE INTO temp_keep_ids SELECT MIN(id) FROM {TABLE_FINANCIALS} WHERE report_date = :rd AND idrssd IN ({rssds_str}) GROUP BY idrssd, concept_reference, context_ref;")
                conn.execute(query_insert, {"rd": rd})
                query_delete = text(f"DELETE t1 FROM {TABLE_FINANCIALS} t1 LEFT JOIN temp_keep_ids t2 ON t1.id = t2.id WHERE t1.report_date = :rd AND t1.idrssd IN ({rssds_str}) AND t2.id IS NULL;")
                result = conn.execute(query_delete, {"rd": rd})
                total_removed += result.rowcount
    with engine.begin() as conn:
        conn.execute(text("DROP TEMPORARY TABLE temp_keep_ids;"))
    yield (f"Success! Removed {total_removed} duplicates.", 1.0, total_removed)

def wipe_period(report_date):
    """Deletes all financial data and logs for a specific period."""
    DB_URL = st.secrets["DB_URL"]
    engine = get_db_engine(DB_URL)
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {TABLE_FINANCIALS} WHERE report_date = :rd"), {"rd": report_date})
        conn.execute(text(f"DELETE FROM {TABLE_LOG} WHERE report_date = :rd"), {"rd": report_date})

def process_xml_worker_by_content(xml_content, report_date):
    """Helper function to parse XML content into list of tuples."""
    rows = []
    try:
        root = ET.fromstring(xml_content)
        idrssd = None
        for elem in root.iter():
            if elem.tag.endswith('identifier') and elem.text:
                try: idrssd = int(elem.text); break
                except: pass
        if not idrssd: return [] 
        for child in root:
            if 'contextRef' in child.attrib:
                concept_ref = child.tag.split('}')[-1]
                value = child.text.strip() if child.text else None
                if value is not None:
                    rows.append((idrssd, report_date, concept_ref, value, child.attrib.get('unitRef'), child.attrib.get('contextRef')))
        return rows
    except: return []

def run_bulk_parse(download_dir):
    """Parses downloaded zip bundles and pushes them to SQL."""
    yield ("Preparing Database...", 0.0)
    setup_database()
    checkpoint = load_checkpoint()
    zip_files = sorted(glob.glob(os.path.join(download_dir, "*.zip")))
    if not zip_files: return
    DB_URL = st.secrets["DB_URL"]
    engine = get_db_engine(DB_URL)
    
    for zip_idx, zip_path in enumerate(zip_files, start=1):
        base_progress = (zip_idx - 1) / len(zip_files)
        match = re.search(r'(\d{8})', os.path.basename(zip_path))
        if not match: continue
        raw_date = match.group(1)
        formatted_date = f"{raw_date[0:2]}/{raw_date[2:4]}/{raw_date[4:8]}"
        
        if formatted_date in checkpoint.get("parsed_folders", {}): continue
        
        with engine.begin() as conn:
            conn.execute(text(f"REPLACE INTO {TABLE_LOG} (report_date, status, records_inserted) VALUES (:rd, 'STARTED', 0)"), {"rd": formatted_date})
            
        yield (f"Processing {formatted_date}...", base_progress)
        
        with zipfile.ZipFile(zip_path, 'r') as z:
            all_files = z.namelist()
            por_filename = next((f for f in all_files if "POR" in f.upper() and f.endswith(".txt")), None)
            
            if por_filename:
                with z.open(por_filename) as f:
                    content = f.read().decode('utf-8', errors='replace')
                    reader = csv.DictReader(content.splitlines(), delimiter='\t')
                    por_records = [{"idrssd": int(row["IDRSSD"]), "bank_name": row["Financial Institution Name"]} 
                                   for row in reader if row.get("IDRSSD") and row.get("Financial Institution Name")]
                    if por_records:
                        with engine.begin() as conn:
                            conn.execute(text(f"REPLACE INTO {TABLE_POR} (idrssd, bank_name) VALUES (:idrssd, :bank_name)"), por_records)
                            
            xml_files = [f for f in all_files if f.endswith(".xml")]
            total_xmls = len(xml_files); batch_buffer = []; BATCH_SIZE = 10000; total_rows_inserted = 0
            
            for i, xml_file in enumerate(xml_files, start=1):
                with z.open(xml_file) as f:
                    batch_buffer.extend(process_xml_worker_by_content(f.read(), formatted_date))
                    
                if len(batch_buffer) >= BATCH_SIZE:
                    with engine.begin() as conn:
                        sql = text(f"INSERT INTO {TABLE_FINANCIALS} (idrssd, report_date, concept_reference, value, unit_ref, context_ref) VALUES (:idrssd, :report_date, :concept_reference, :value, :unit_ref, :context_ref)")
                        conn.execute(sql, [{"idrssd": r[0], "report_date": r[1], "concept_reference": r[2], "value": r[3], "unit_ref": r[4], "context_ref": r[5]} for r in batch_buffer])
                    total_rows_inserted += len(batch_buffer)
                    batch_buffer = []
                    
                if i % 100 == 0:
                    yield (f"({zip_idx}/{len(zip_files)}) Parsing {formatted_date}: {i}/{total_xmls} files...", base_progress + (i / total_xmls) / len(zip_files))
                    
            if batch_buffer:
                with engine.begin() as conn:
                    sql = text(f"INSERT INTO {TABLE_FINANCIALS} (idrssd, report_date, concept_reference, value, unit_ref, context_ref) VALUES (:idrssd, :report_date, :concept_reference, :value, :unit_ref, :context_ref)")
                    conn.execute(sql, [{"idrssd": r[0], "report_date": r[1], "concept_reference": r[2], "value": r[3], "unit_ref": r[4], "context_ref": r[5]} for r in batch_buffer])
                total_rows_inserted += len(batch_buffer)
                
        # Properly Indented Block for marking completion
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE {TABLE_LOG} SET status = 'COMPLETED', records_inserted = :ri WHERE report_date = :rd"), {"ri": total_rows_inserted, "rd": formatted_date})
            
    yield ("Done!", 1.0)

def get_date_objects(date_str):
    """Helper for run_bulk_download to parse date strings."""
    try:
        return datetime.strptime(date_str, "%m/%d/%Y")
    except:
        return None

def run_bulk_download(start_date_str, end_date_str, download_dir, mode="range"):
    """Drives Selenium to download the zip bundles."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    
    yield ("Step 1: Configuring Headless Browser...", 0.0)
    chrome_options = Options()
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    prefs = {"download.default_directory": download_dir}
    chrome_options.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 30)
    
    try:
        yield ("Step 2: Accessing FFIEC...", 0.05)
        driver.get("https://cdr.ffiec.gov/public/pws/downloadbulkdata.aspx")
        product_dropdown = wait.until(EC.presence_of_element_located((By.ID, "ListBox1")))
        Select(product_dropdown).select_by_visible_text("Call Reports -- Single Period")
        time.sleep(2) 
        
        xbrl_radio = wait.until(EC.element_to_be_clickable((By.ID, "XBRLRadiobutton")))
        driver.execute_script("arguments[0].click();", xbrl_radio)
        time.sleep(2) 
        
        date_dropdown_el = wait.until(EC.presence_of_element_located((By.ID, "DatesDropDownList")))
        wait.until(lambda d: len(Select(date_dropdown_el).options) > 1) 
        all_options = [opt.text.strip() for opt in Select(date_dropdown_el).options if opt.text.strip()]
        
        target_dates = []
        if mode == "range":
            start_dt = datetime.strptime(start_date_str, "%m/%d/%Y")
            end_dt = datetime.strptime(end_date_str, "%m/%d/%Y")
            target_dates = [opt for opt in all_options if get_date_objects(opt) and (start_dt <= get_date_objects(opt) <= end_dt)]
        else:
            checkpoint = load_checkpoint()
            parsed_keys = checkpoint.get("parsed_folders", {}).keys()
            target_dates = [opt for opt in all_options if get_date_objects(opt) and opt not in parsed_keys]
            
        if not target_dates:
            yield ("No new data to download.", 1.0); return
            
        for idx, target in enumerate(target_dates):
            current_progress = (idx / len(target_dates)) * 0.9 + 0.1
            yield (f"Downloading {target}...", current_progress)
            Select(driver.find_element(By.ID, "DatesDropDownList")).select_by_visible_text(target)
            time.sleep(1)
            driver.execute_script("arguments[0].click();", driver.find_element(By.ID, "Download_0"))
            time.sleep(2)
            while glob.glob(os.path.join(download_dir, "*.crdownload")): time.sleep(2)
            
        yield ("Downloads complete.", 1.0)
        
    except Exception as e: 
        yield (f"Error: {e}", 1.0)
    finally: 
        driver.quit()