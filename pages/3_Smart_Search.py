import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from google import genai
import requests
import re
import time
import socket

# --- UI Setup ---
st.set_page_config(page_title="Smart-Search (beta)", layout="wide")

st.markdown("""
    <style>
    .stTextArea textarea {
        border: 1px solid #cccccc !important;
        border-radius: 6px !important;
        transition: all 0.2s ease-in-out;
        font-family: 'Inter', sans-serif;
    }
    .stTextArea textarea:focus {
        border: 1px solid #115740 !important;
        box-shadow: 0 0 8px rgba(17, 87, 64, 0.15) !important;
    }
    div.stButton > button:first-child {
        background-color: #115740;
        color: white;
        border-radius: 6px;
        height: 2.8em;
        font-weight: 500;
        transition: 0.2s;
        border: none;
    }
    div.stButton > button:first-child:hover {
        background-color: #0d4533;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }
    </style>
    """, unsafe_allow_html=True)

st.markdown("<h1 style='text-align: center; color: #115740; font-weight: 600;'>Call Report Smart Analyst</h1>", unsafe_allow_html=True)
st.markdown("<hr style='border: 1px solid #eaeaea; margin-top: 0;'>", unsafe_allow_html=True)

# --- Database Connection ---
DB_URL = st.secrets["DB_URL"]
if "/test" in DB_URL:
    DB_URL = DB_URL.replace("/test", "/ffiec_data")

connect_args = {}
if "tidbcloud.com" in DB_URL:
    connect_args = {"ssl": {"fake_config": True}}
engine = create_engine(DB_URL, connect_args=connect_args)

# --- Helper: Check if Ollama is running ---
def is_ollama_online(url="localhost", port=11434):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex((url, port)) == 0
    except:
        return False

# --- Configuration Sidebar ---
with st.sidebar:
    st.header("Engine Settings")
    search_engine = st.radio("Select Search Engine:", ["Gemini (Cloud)", "Ollama (Local)"])
    
    if search_engine == "Gemini (Cloud)":
        try:
            api_key = st.secrets["GEMINI_API_KEY"]
            st.success("Gemini Key loaded successfully.")
        except:
            api_key = st.text_input("Enter Gemini API Key", type="password")
        model_choice = st.selectbox("Model:", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"])
    else:
        if is_ollama_online():
            st.success("Ollama Service: ONLINE")
            try:
                tags = requests.get("http://localhost:11434/api/tags").json()
                models = [m['name'] for m in tags.get('models', [])]
                model_choice = st.selectbox("Local Model:", models if models else ["llama3"])
            except:
                model_choice = st.text_input("Model Name:", value="llama3")
        else:
            st.error("Ollama Service: OFFLINE")
            st.info("Run `brew services start ollama` in terminal.")
            model_choice = "llama3"
        
        ollama_url = st.text_input("Ollama Endpoint URL:", value="http://localhost:11434/api/generate")

    st.divider()
    if st.button("Clear Application Cache"):
        st.cache_data.clear()
        st.success("Cache Cleared.")

# --- Helper: Updated Schema & Rules ---
def get_schema_context():
    return """
    MYSQL SCHEMA:
    1. Table: 'call_reports_financials' (idrssd, report_date, concept_reference, value)
    2. Table: 'call_reports_por' (idrssd, bank_name) - Use this for bank names.
    3. Table: 'mdrm_dictionary' (concept_reference, item_name) - Use this for field definitions.
    
    JOIN RULES:
    - Join 'call_reports_financials' (f) and 'call_reports_por' (p) on f.idrssd = p.idrssd.
    - Join 'call_reports_financials' (f) and 'mdrm_dictionary' (d) on f.concept_reference = d.concept_reference.

    EXAMPLES:
    User: Show me the top 5 banks by Total Assets (RCFD2170) for 12312023.
    SQL: SELECT p.bank_name, f.value FROM call_reports_financials f JOIN call_reports_por p ON f.idrssd = p.idrssd WHERE f.concept_reference = 'RCFD2170' AND f.report_date = '12312023' ORDER BY f.value DESC LIMIT 5;

    RULES:
    - Return ONLY raw SQL starting with SELECT.
    - Use exact match for report_date = 'MMDDYYYY'.
    - Use ORDER BY RAND() for random sorting.
    """

# --- AI Logic Wrapper ---
@st.cache_resource
def get_ai_client(api_key):
    return genai.Client(api_key=api_key)

def get_ai_response(client, model_id, prompt, schema):
    full_prompt = f"{schema}\n\nUser Question/Context: {prompt}"
    # Exponential backoff: 2s, 4s, 8s
    for attempt in range(3):
        try:
            return client.models.generate_content(model=model_id, contents=full_prompt).text
        except Exception as e:
            if attempt == 2: raise e
            sleep_time = 2 ** (attempt + 1)
            time.sleep(sleep_time)
    return ""

# --- UI Tabs ---
tab1, tab2 = st.tabs(["AI Smart Search", "Database Statistics"])

with tab2:
    st.subheader("Database Overview")
    try:
        with engine.connect() as conn:
            count_rows = conn.execute(text("SELECT COUNT(*) FROM call_reports_financials")).scalar()
            st.metric("Financial Records Indexed", f"{count_rows:,}")
            st.write("### Financial Data Sample")
            sample = pd.read_sql(text("SELECT * FROM call_reports_financials LIMIT 5"), conn)
            st.dataframe(sample, use_container_width=True)
    except Exception as e:
        st.error(f"Error loading statistics: {e}")

with tab1:
    st.subheader("Query Interface")
    user_query = st.text_area(
        "Enter your query parameters:", 
        placeholder="e.g., Which banks reported the highest Total Assets (RCFD2170) on 12312023?",
    )

    if st.button("Execute Analysis", use_container_width=True):
        if search_engine == "Gemini (Cloud)" and not api_key:
            st.error("API Key required.")
        elif not user_query:
            st.warning("Please enter a question.")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()
            start_t = time.time()
            try:
                # 1. GENERATE SQL
                status_text.info("Generating SQL...")
                progress_bar.progress(25)
                
                # Note: We must handle local Ollama vs Gemini logic here now
                if search_engine == "Gemini (Cloud)":
                    raw_sql = get_ai_response(get_ai_client(api_key), model_choice, user_query, get_schema_context())
                else:
                    payload = {"model": model_choice, "prompt": f"{get_schema_context()}\n\nUser Question/Context: {user_query}", "stream": False}
                    # Increased timeout to 10 minutes to support slow local hardware
                    raw_sql = requests.post(ollama_url, json=payload, timeout=600).json().get("response", "")
                
                # Clean SQL
                clean_sql = re.sub(r"```sql\n?|```", "", raw_sql).strip()
                if not clean_sql.upper().startswith("SELECT"):
                    match = re.search(r"SELECT.*", clean_sql, re.DOTALL | re.IGNORECASE)
                    clean_sql = match.group(0) if match else clean_sql

                st.markdown("**Generated SQL Query:**")
                st.code(clean_sql, language="sql")

                # 2. RUN QUERY
                status_text.info("Executing query...")
                progress_bar.progress(50)
                
                with engine.connect() as conn:
                    df = pd.read_sql(text(clean_sql), conn)

                if df.empty:
                    st.warning("No records found.")
                else:
                    # 3. SUMMARIZE
                    status_text.info("Summarizing results...")
                    progress_bar.progress(75)
                    
                    summary_prompt = f"Summarize this data for the user: {df.head(5).to_string()}"
                    if search_engine == "Gemini (Cloud)":
                        answer = get_ai_response(get_ai_client(api_key), model_choice, summary_prompt, "Be a helpful financial analyst.")
                    else:
                        payload = {"model": model_choice, "prompt": f"Be a helpful financial analyst.\n\n{summary_prompt}", "stream": False}
                        answer = requests.post(ollama_url, json=payload, timeout=180).json().get("response", "")
                    
                    progress_bar.progress(100)
                    status_text.empty()
                    
                    st.markdown("### Analyst Summary")
                    st.info(answer)
                    st.dataframe(df, use_container_width=True)

            except Exception as e:
                st.error(f"Analysis Failed: {e}")
