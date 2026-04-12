import streamlit as st
import os
import shutil
import datetime
import json
import re
from sqlalchemy import create_engine, text

# Import the background processing engine
from update_engine import run_bulk_download, run_bulk_parse

def get_latest_parsed_date():
    """Queries the database to find the most recently parsed Call Report date."""
    try:
        DB_URL = st.secrets["DB_URL"]
        
        # TiDB Cloud requires SSL. Pymysql uses 'ssl_ca' or 'ssl' dict.
        # If using pymysql, we pass connect_args
        connect_args = {}
        if "tidbcloud.com" in DB_URL:
            connect_args = {"ssl": {"fake_config": True}} # Standard placeholder for many providers
            
        engine = create_engine(DB_URL, connect_args=connect_args)
        with engine.connect() as conn:
            # Query the max report_date from the financials table
            result = conn.execute(text("SELECT MAX(report_date) FROM call_reports_financials")).scalar()
            if result:
                # Handle both string (MMDDYYYY) and date/datetime objects
                if isinstance(result, (datetime.date, datetime.datetime)):
                    return result if isinstance(result, datetime.date) else result.date()
                
                # If it's a string like '12312023'
                match = re.search(r'(\d{8})', str(result))
                if match:
                    return datetime.datetime.strptime(match.group(1), "%m%d%Y").date()
    except Exception:
        pass # Fall back to today's date if DB is empty or unreachable
        
    return datetime.date.today()

# Setup the Home Page
st.set_page_config(page_title="FFIEC Toolkit", page_icon="🦅", layout="centered")

# W&M styled header
st.markdown("<h1 style='text-align: center; color: #115740;'>🦅 FFIEC Toolkit</h1>", unsafe_allow_html=True)
st.markdown("<h4 style='text-align: center; color: #222222; margin-bottom: 40px;'>Centralized Financial Institution Data Explorer</h4>", unsafe_allow_html=True)

st.write("Welcome to the FFIEC Toolkit. Use the modules below to query historical call reports, extract XBRL tags, or update your local database.")

st.markdown("---")

# ==========================================
# TOOL 1: FETCH TOOL (Existing)
# ==========================================
st.subheader("1. API Fetch Tool")
st.write("Query the local database for historical call reports and definitions.")

col1, col2, col3 = st.columns([1, 2, 1])
with col2:
    if st.button("Launch Fetch Tool", type="primary", use_container_width=True):
        st.switch_page("pages/1_Fetch_Tool.py")

st.markdown("---")

# ==========================================
# TOOL 2: UPDATE DATABASE TOOL (New)
# ==========================================
st.subheader("2. Bulk Update Database Tool")
st.write("Download historical .zip bundles from the FFIEC website and parse them directly into the local SQL database.")

# --- UPDATE MODE TOGGLE ---
update_mode = st.radio("Select Update Mode:", 
                       ["Smart Catch-up (Auto-detect missing updates)", "Specific Date Range"],
                       horizontal=True)

if update_mode == "Specific Date Range":
    mode_flag = "range"
    
    # Configuration for our Quarter dropdowns
    QUARTER_MAP = {
        "Q1 (March 31)": (3, 31),
        "Q2 (June 30)": (6, 30),
        "Q3 (September 30)": (9, 30),
        "Q4 (December 31)": (12, 31)
    }
    quarter_options = list(QUARTER_MAP.keys())
    
    # Configuration for our Year dropdowns (Current Year down to 2001)
    current_year = datetime.date.today().year
    year_options = list(range(current_year, 2000, -1))
    
    # Get the smart default from JSON to pre-populate the dropdowns
    default_date = get_latest_parsed_date()
    default_year = default_date.year if default_date.year in year_options else current_year
    
    # Determine which quarter the default date falls into
    if default_date.month <= 3: default_q_idx = 0
    elif default_date.month <= 6: default_q_idx = 1
    elif default_date.month <= 9: default_q_idx = 2
    else: default_q_idx = 3

    # Layout: 4 columns side-by-side for Start and End selections
    st.markdown("##### Select Date Range")
    c1, c2, c3, c4 = st.columns(4)
    
    with c1:
        start_year = st.selectbox("Start Year", year_options, index=year_options.index(default_year))
    with c2:
        start_q = st.selectbox("Start Quarter", quarter_options, index=default_q_idx)
    with c3:
        end_year = st.selectbox("End Year", year_options, index=year_options.index(default_year))
    with c4:
        end_q = st.selectbox("End Quarter", quarter_options, index=default_q_idx)
        
    # Translate the dropdown selections back into actual datetime objects
    start_month, start_day = QUARTER_MAP[start_q]
    start_date = datetime.date(start_year, start_month, start_day)
    
    end_month, end_day = QUARTER_MAP[end_q]
    end_date = datetime.date(end_year, end_month, end_day)

    # Validation: Ensure End Date is >= Start Date
    if start_date > end_date:
        st.error(f"Invalid Range: End Date ({end_date.strftime('%m/%d/%Y')}) cannot be before Start Date ({start_date.strftime('%m/%d/%Y')}).")
        st.stop() # Halts script execution so the download button won't run

else:
    mode_flag = "smart"
    st.info("**Smart Catch-up:** The tool will scan the FFIEC server, compare the available dates to your local database, and automatically download/parse only the reports you are missing.")
    start_date = None
    end_date = None

if st.button("Start Bulk Download & Parse", use_container_width=True):
    # Setup Temporary Directory
    TEMP_DIR = os.path.join(os.getcwd(), "temp_bulk_downloads")
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
    
    # Setup UI Elements for Progress
    status_text = st.empty()
    progress_bar = st.progress(0.0)
    
    try:
        # --- PHASE 1: DOWNLOADING ---
        # The datetime objects from our dropdowns are formatted exactly how update_engine expects them
        str_start = start_date.strftime("%m/%d/%Y") if mode_flag == "range" else None
        str_end = end_date.strftime("%m/%d/%Y") if mode_flag == "range" else None
        
        status_text.info("Starting browser for download...")
        for status_msg, progress_pct in run_bulk_download(str_start, str_end, TEMP_DIR, mode=mode_flag):
            status_text.text(f"Downloading: {status_msg}")
            progress_bar.progress(progress_pct * 0.5) 
            
        # --- PHASE 2: PARSING & SQL PUSH ---
        status_text.info("Downloads complete. Starting XML parsing and SQL insertion...")
        for status_msg, progress_pct in run_bulk_parse(TEMP_DIR):
            status_text.text(f"Parsing: {status_msg}")
            progress_bar.progress(0.5 + (progress_pct * 0.5))

        # --- COMPLETION ---
        progress_bar.progress(1.0)
        status_text.success("Database successfully updated!")
        st.balloons()

    except Exception as e:
        status_text.error(f"An error occurred: {e}")

    finally:
        # Cleanup
        status_text.write("Cleaning up temporary files...")
        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
        status_text.write("Cleanup complete. Ready for next task.")
st.markdown("---")


# ==========================================
# TOOL 3: Smart Search (beta)
# ==========================================
st.subheader("3. LLM Assist")
st.write("Query the local database using Natural Language prompts.")

col1, col2, col3 = st.columns([1, 2, 1])
with col2:
    if st.button("Launch LLM Tool", type="primary", use_container_width=True):
        st.switch_page("pages/3_Smart_Search.py")

st.markdown("---")