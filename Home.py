import streamlit as st
import os
import shutil
import datetime
import json
import re
from sqlalchemy import create_engine, text

from update_engine import run_bulk_download, run_bulk_parse, deduplicate_data, wipe_period, get_db_engine, setup_database

def get_latest_parsed_date():
    try:
        DB_URL = st.secrets["DB_URL"]
        engine = get_db_engine(DB_URL)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT MAX(report_date) FROM migration_log WHERE status = 'COMPLETED'")).scalar()
            if result:
                match = re.search(r'(\d{2})/(\d{2})/(\d{4})', str(result))
                if match:
                    m, d, y = match.groups()
                    return datetime.date(int(y), int(m), int(d))
    except Exception:
        pass 
        
    return datetime.date.today()

st.set_page_config(page_title="FFIEC Toolkit", page_icon="🦅", layout="centered")

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
    
    QUARTER_MAP = {
        "Q1 (March 31)": (3, 31),
        "Q2 (June 30)": (6, 30),
        "Q3 (September 30)": (9, 30),
        "Q4 (December 31)": (12, 31)
    }
    quarter_options = list(QUARTER_MAP.keys())
    
    current_year = datetime.date.today().year
    year_options = list(range(current_year, 2000, -1))
    
    default_date = get_latest_parsed_date()
    default_year = default_date.year if default_date.year in year_options else current_year
    
    if default_date.month <= 3: default_q_idx = 0
    elif default_date.month <= 6: default_q_idx = 1
    elif default_date.month <= 9: default_q_idx = 2
    else: default_q_idx = 3

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
        
    start_month, start_day = QUARTER_MAP[start_q]
    start_date = datetime.date(start_year, start_month, start_day)
    
    end_month, end_day = QUARTER_MAP[end_q]
    end_date = datetime.date(end_year, end_month, end_day)

    if start_date > end_date:
        st.error(f"Invalid Range: End Date ({end_date.strftime('%m/%d/%Y')}) cannot be before Start Date ({start_date.strftime('%m/%d/%Y')}).")
        st.stop()

else:
    mode_flag = "smart"
    st.info("**Smart Catch-up:** The tool will scan the FFIEC server, compare the available dates to your local database, and automatically download/parse only the reports you are missing.")
    start_date = None
    end_date = None

if st.button("Start Bulk Download & Parse", use_container_width=True):
    TEMP_DIR = os.path.join(os.getcwd(), "temp_bulk_downloads")
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
    
    status_text = st.empty()
    progress_bar = st.progress(0.0)
    
    try:
        # --- PHASE 1: DOWNLOADING ---
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
        status_text.error(f"❌ Process Failed: {e}")
        st.exception(e)

    finally:
        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
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

# ==========================================
# TOOL 4: DATABASE MAINTENANCE
# ==========================================
st.subheader("🛠️ Database Maintenance")
st.write("Manage database health, deduplicate records, or reset specific periods.")

with st.expander("Show Maintenance Tools"):
    m_col1, m_col2 = st.columns(2)

    with m_col1:
        st.markdown("### 🧹 Clean Data")
        st.write("Remove exact duplicate records from the financials table to ensure data integrity.")
        st.markdown("<div style='height: 45px;'></div>", unsafe_allow_html=True)

        if st.button("Run Global Deduplication", use_container_width=True):
            status_box = st.empty()
            prog_bar = st.progress(0.0)
            try:
                for msg, prog, count in deduplicate_data():
                    status_box.info(msg)
                    prog_bar.progress(prog)
                st.success(f"Deduplication complete!")
            except Exception as e:
                st.error(f"Deduplication failed: {e}")

    with m_col2:
        st.markdown("### 🔄 Reset Data")
        st.write("Wipe all data for a specific period. Use this if a download was corrupted or partial.")

        date_options = []
        with st.spinner("🔍 Checking database for available periods..."):
            try:
                setup_database()
                
                DB_URL = st.secrets["DB_URL"]
                engine = get_db_engine(DB_URL)
                with engine.connect() as conn:
                    all_dates = set()
                    
                    try:
                        res = conn.execute(text("SELECT DISTINCT report_date FROM call_reports_financials")).fetchall()
                        for r in res: all_dates.add(r[0])
                    except: pass
                    
                    try:
                        res = conn.execute(text("SELECT DISTINCT report_date FROM migration_log")).fetchall()
                        for r in res: all_dates.add(r[0])
                    except: pass
                    
                    date_options = sorted(list(all_dates), reverse=True)
            except Exception:
                pass

        selected_wipe = st.selectbox("Select Period", options=date_options if date_options else ["No data found"])
        
        if not date_options:
            st.info("💡 No report dates found in the database. Run a download first!")

        if st.button("Wipe Selected Period", type="secondary", use_container_width=True, disabled=not date_options):
            if selected_wipe and selected_wipe != "No data found":
                with st.spinner(f"Wiping {selected_wipe}..."):
                    wipe_period(selected_wipe)
                    st.success(f"Reset {selected_wipe}!")
                    st.rerun()
