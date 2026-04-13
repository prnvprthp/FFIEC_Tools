import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
import h2o
from h2o.automl import H2OAutoML
import time

# --- Configuration & DB Connection ---
st.set_page_config(page_title="AutoML Predictor", page_icon="🧠", layout="wide")

DB_URL = st.secrets["DB_URL"]
if "/test" in DB_URL:
    DB_URL = DB_URL.replace("/test", "/ffiec_data")

@st.cache_resource
def get_db_engine():
    connect_args = {}
    if "tidbcloud.com" in DB_URL:
        connect_args = {"ssl": {"fake_config": True}}
    return create_engine(DB_URL, connect_args=connect_args, pool_pre_ping=True)

try:
    engine = get_db_engine()
except Exception as e:
    st.error(f"Failed to create database engine: {e}")
    st.stop()

# --- Helper Functions ---
def format_period(period_str):
    period_str = str(period_str)
    if len(period_str) != 8:
        return period_str
    month = period_str[0:2]
    year = period_str[4:8]
    q_map = {'03': 'Q1', '06': 'Q2', '09': 'Q3', '12': 'Q4'}
    return f"{year} {q_map.get(month, 'Q?')}"

@st.cache_data
def get_mdrm_mapping():
    try:
        with engine.connect() as conn:
            df = pd.read_sql(text("SELECT concept_reference, item_name FROM mdrm_dictionary"), conn)
        df['formatted_name'] = df['item_name'] + " (" + df['concept_reference'] + ")"
        return dict(zip(df['concept_reference'], df['formatted_name']))
    except Exception as e:
        st.warning(f"Could not load MDRM dictionary from DB, falling back to raw codes: {e}")
        return {}

@st.cache_data(ttl=3600)
def get_filter_options():
    with engine.connect() as conn:
        banks_df = pd.read_sql(text("SELECT DISTINCT idrssd, bank_name FROM call_reports_por ORDER BY bank_name"), conn)
        periods_df = pd.read_sql(text("SELECT DISTINCT report_date FROM call_reports_financials ORDER BY report_date DESC"), conn)
    return banks_df, periods_df['report_date'].tolist()

@st.cache_data(show_spinner=False)
def fetch_and_pivot_data(selected_idrssds, selected_periods):
    idrssd_list = ",".join([str(i) for i in selected_idrssds])
    period_list = ",".join([f"'{p}'" for p in selected_periods])
    
    query = f"""
        SELECT idrssd, report_date, concept_reference, value 
        FROM call_reports_financials 
        WHERE idrssd IN ({idrssd_list}) 
        AND report_date IN ({period_list})
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(query), conn)
    
    if df.empty:
        return df
        
    pivot_df = df.pivot_table(
        index=['idrssd', 'report_date'], 
        columns='concept_reference', 
        values='value', 
        aggfunc='first'
    ).reset_index()
    
    mapping = get_mdrm_mapping()
    pivot_df = pivot_df.rename(columns=lambda x: mapping.get(x, x))
    
    return pivot_df

# --- UI Setup ---
st.title("🧠 Predictive Analytics (AutoML)")
st.write("Filter a specific dataset, define your target variable, and let H2O.ai find the best machine learning model.")

# --- Step 1: Dataset Slicer ---
st.markdown("### 1. Define Dataset Scope")
banks_df, available_periods = get_filter_options()

col1, col2 = st.columns(2)
with col1:
    bank_options = banks_df.apply(lambda x: f"{x['bank_name']} ({x['idrssd']})", axis=1).tolist()
    selected_banks_str = st.multiselect("Select Banks (Leave empty for a random sample of 1000)", bank_options, max_selections=50)
    
    selected_idrssds = []
    if selected_banks_str:
        selected_idrssds = [int(b.split("(")[-1].replace(")", "")) for b in selected_banks_str]
    else:
        sample_size = min(1000, len(banks_df))
        selected_idrssds = banks_df['idrssd'].sample(n=sample_size, random_state=42).tolist()
        st.warning(f"No banks selected. Defaulting to a random sample of {sample_size} banks.")

with col2:
    selected_periods = st.multiselect(
        "Select Reporting Periods", 
        available_periods, 
        default=available_periods[:2] if len(available_periods) > 1 else available_periods,
        format_func=format_period
    )

if st.button("Load Dataset", type="primary"):
    if not selected_periods:
        st.error("Please select at least one reporting period.")
    else:
        with st.spinner("Extracting and reshaping data from SQL..."):
            df_ml = fetch_and_pivot_data(selected_idrssds, selected_periods)
            if df_ml.empty:
                st.error("No data found for this combination.")
            else:
                st.session_state['ml_data'] = df_ml
                st.success(f"Data loaded successfully! Shape: {df_ml.shape[0]} observations, {df_ml.shape[1]} features.")

# --- Step 2: AutoML Setup ---
if 'ml_data' in st.session_state:
    df_ml = st.session_state['ml_data']
    st.markdown("---")
    st.markdown("### 2. Configure Machine Learning Task")
    
    task_type = st.radio("Select Task Type", ["Regression (Predict a Financial Number)", "Classification (Predict a Category/Status)"], horizontal=True)
    
    REGRESSION_IDS = ['RCON2170', 'RCON2948', 'RCON2200', 'RCON1400', 'RCON3210', 'RIAD4340', 'RIAD4000', 'RIAD4073', 'RCON3123', 'RCONP793']
    CLASSIFICATION_IDS = ['RCONC410', 'RCONC411', 'RIAD0116', 'RCONC225', 'RCONG476', 'RCONJ058', 'RCONF076', 'RCONC435', 'RCONA530', 'RCONP782']
    
    available_cols = df_ml.columns.tolist()
    if "Regression" in task_type:
        target_pool = [col for col in available_cols if any(rid in col for rid in REGRESSION_IDS)]
    else:
        target_pool = [col for col in available_cols if any(cid in col for cid in CLASSIFICATION_IDS)]
    
    col3, col4 = st.columns([2, 1])
    with col3:
        if not target_pool:
            st.warning(f"None of the predefined {task_type.split()[0]} targets were found. Showing all available features.")
            target_pool = available_cols[2:] 
            
        target_col = st.selectbox("Select Target Variable to Predict", options=target_pool)
    
    with col4:
        max_runtime = st.slider("Max AutoML Runtime (seconds)", min_value=30, max_value=600, value=60, step=30)
        
    st.dataframe(df_ml.head(), use_container_width=True)
    
    # --- Step 3: Execution ---
    if st.button("🚀 Train Models with H2O.ai", use_container_width=True):
        st.markdown("---")
        st.markdown("### 3. Model Leaderboard")
        
        progress_text = st.empty()
        progress_text.info("Initializing H2O Cluster...")
        
        try:
            h2o.init()
            h2o.no_progress() 
            
            progress_text.info("Converting data to H2O In-Memory Frame...")
            hf = h2o.H2OFrame(df_ml)
            
            if "Classification" in task_type:
                hf[target_col] = hf[target_col].asfactor()
                
            predictors = hf.columns
            predictors.remove(target_col)
            if 'idrssd' in predictors: predictors.remove('idrssd')
            if 'report_date' in predictors: predictors.remove('report_date')
            
            train, test = hf.split_frame(ratios=[0.8], seed=42)
            
            progress_text.info(f"Running AutoML for up to {max_runtime} seconds...")
            aml = H2OAutoML(max_models=10, seed=1, max_runtime_secs=max_runtime)
            aml.train(x=predictors, y=target_col, training_frame=train)
            
            progress_text.success("Training Complete!")
            
            st.write("#### Best Performing Models")
            leaderboard_df = aml.leaderboard.as_data_frame()
            st.dataframe(leaderboard_df, use_container_width=True)
            
            best_model = aml.leader
            st.write(f"#### Top Drivers (Variable Importance) for {best_model.algo.upper()}")
            
            try:
                varimp_df = pd.DataFrame(best_model.varimp(), columns=["Feature", "Relative Importance", "Scaled Importance", "Percentage"])
                st.bar_chart(varimp_df.set_index("Feature")["Percentage"].head(15))
            except Exception:
                st.info("Variable importance not available for this specific model type (e.g., Stacked Ensembles).")

        except Exception as e:
            st.error(f"H2O Error: {e}")
        finally:
            if st.button("Shutdown H2O Cluster (Free Memory)"):
                h2o.cluster().shutdown()
                st.success("Cluster shut down.")
