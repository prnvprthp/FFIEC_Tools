import streamlit as st
import pandas as pd
import re
import requests
import base64
from datetime import datetime
from sqlalchemy import create_engine, text

# --- Configuration & DB Connection ---
st.set_page_config(page_title="Fetch Tool", page_icon="🏦", layout="wide")

DB_URL = st.secrets["DB_URL"]

# --- FFIEC API Credentials ---
API_USERNAME = st.secrets.get("FFIEC_USERNAME", "pprathap")
API_TOKEN = st.secrets.get("FFIEC_TOKEN", "")

@st.cache_resource
def get_db_engine():
    return create_engine(DB_URL, pool_pre_ping=True)

try:
    engine = get_db_engine()
except Exception as e:
    st.error(f"Failed to create database engine: {e}")
    st.stop()

# --- Cached Data Fetching Functions ---
@st.cache_data(ttl=3600)
def fetch_all_banks():
    with engine.connect() as conn:
        # PULL FROM NEW POR TABLE
        query = text("""
            SELECT DISTINCT idrssd, bank_name 
            FROM call_reports_por 
            WHERE bank_name IS NOT NULL
            ORDER BY bank_name
        """)
        return pd.read_sql(query, conn)

@st.cache_data(ttl=3600)
def fetch_periods_for_banks(rssd_list):
    """Fetches periods available for the selected banks."""
    if not rssd_list:
        return pd.DataFrame()
    
    rssd_str = ",".join(map(str, rssd_list))
    
    with engine.connect() as conn:
        # USE NEW TABLE AND REPORT_DATE
        query = text(f"""
            SELECT DISTINCT report_date AS source_folder
            FROM call_reports_financials 
            WHERE idrssd IN ({rssd_str})
            ORDER BY report_date DESC
        """)
        return pd.read_sql(query, conn)

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_pdf_from_api(rssd, date_str):
    """Fetches the official Call Report PDF from the FFIEC Azure API."""
    api_url = "https://ffieccdr.azure-api.us/public/RetrieveFacsimile"
    
    headers = {
        "UserID": API_USERNAME,
        "Authentication": f"Bearer {API_TOKEN}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "dataSeries": "Call",
        "reportingPeriodEndDate": date_str,
        "fiIdType": "ID_RSSD",
        "fiId": str(rssd),
        "facsimileFormat": "PDF"
    }
    
    try:
        response = requests.get(api_url, headers=headers, timeout=15)
        
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            if 'application/json' in content_type.lower():
                try:
                    data = response.json()
                    if isinstance(data, str):
                        return base64.b64decode(data), None
                    elif isinstance(data, dict):
                        if "FacsimileFile" in data:
                            return base64.b64decode(data["FacsimileFile"]), None
                        else:
                            return None, f"Unexpected dictionary keys: {list(data.keys())}"
                    else:
                        return None, f"Unexpected data type returned: {type(data)}"
                except Exception as e:
                    return None, f"JSON/Decode Error: {str(e)}"
            else:
                return response.content, None
                
        return None, f"API Error {response.status_code}: {response.text}"
    except Exception as e:
        return None, f"Connection Error: {str(e)}"

# --- Helper Formatting Functions ---
def format_date_label(folder_name):
    match = re.search(r'\d{8}', str(folder_name))
    if match:
        date_str = match.group(0)
        try:
            return datetime.strptime(date_str, "%m%d%Y").strftime("%m/%d/%Y")
        except ValueError:
            try:
                return datetime.strptime(date_str, "%Y%m%d").strftime("%m/%d/%Y")
            except ValueError:
                return str(folder_name)
    return str(folder_name)

def format_value_column(row):
    val = row['Value']
    unit = row['Unit']
    if pd.notna(val) and unit == 'USD':
        try:
            return f"${float(val):,.2f}".replace(".00", "")
        except (ValueError, TypeError):
            return val
    return val

# --- UI Setup ---
st.title("FFIEC Call Report Fetcher")

try:
    banks_df = fetch_all_banks()

    if banks_df.empty:
        st.warning("No data found in the `call_reports` table. Please run your parser first.")
    else:
        bank_map = dict(zip(banks_df['bank_name'] + " (ID: " + banks_df['idrssd'].astype(str) + ")", banks_df['idrssd']))
        rssd_to_name = dict(zip(banks_df['idrssd'], banks_df['bank_name']))

        # --- STEP 1: Multi-Select ---
        selected_bank_labels = st.multiselect(
            "1. Search and Select Financial Institution(s)",
            options=list(bank_map.keys()),
            placeholder="Choose one or more banks to compare..."
        )

        # --- STEP 2: Period Selection (Multi-Select) ---
        if selected_bank_labels:
            selected_rssds = [bank_map[label] for label in selected_bank_labels]
            dates_df = fetch_periods_for_banks(selected_rssds)
            
            selected_periods = st.multiselect(
                "2. Select Reporting Period(s)", 
                options=dates_df['source_folder'].astype(str).tolist(),
                format_func=format_date_label,
                placeholder="Choose one or more time periods to compare..."
            )

            # --- STEP 3: Data Fetching ---
            if selected_periods:
                # Sort periods chronologically
                selected_periods.sort()
                
                # Setup session state to remember the button click
                if "fetch_clicked" not in st.session_state:
                    st.session_state.fetch_clicked = False
                if "last_selection" not in st.session_state:
                    st.session_state.last_selection = None
                
                # Reset the view if the user changes their bank or date selection
                current_selection = f"{','.join(map(str, selected_rssds))}_{','.join(selected_periods)}"
                if st.session_state.last_selection != current_selection:
                    st.session_state.fetch_clicked = False
                    st.session_state.last_selection = current_selection
                
                if st.button("Fetch Data"):
                    st.session_state.fetch_clicked = True
                
                if st.session_state.fetch_clicked:
                    with st.spinner("Querying database & fetching PDFs..."):
                        
                        # --- SQL Data Fetch ---
                        with engine.connect() as conn:
                            rssd_str = ",".join(map(str, selected_rssds))
                            periods_str = ",".join([f"'{p}'" for p in selected_periods])
                            
                            # UPDATED FOR NEW SCHEMA
                            data_query = text(f"""
                                SELECT 
                                    f.idrssd AS "RSSD",
                                    f.report_date AS "Period",
                                    f.concept_reference AS "Field ID", 
                                    COALESCE(d.item_name, 'Definition not available') AS "Definition",
                                    f.value AS "Value", 
                                    f.unit_ref AS "Unit",
                                    f.context_ref AS "Context"
                                FROM call_reports_financials f
                                LEFT JOIN mdrm_dictionary d 
                                    ON f.concept_reference = d.concept_reference 
                                WHERE f.idrssd IN ({rssd_str}) AND f.report_date IN ({periods_str})
                                ORDER BY f.concept_reference ASC
                            """)
                            report_data = pd.read_sql(data_query, conn)
                    
                    if not report_data.empty:
                        # Format values before pivoting
                        report_data['Value'] = report_data.apply(format_value_column, axis=1)
                        
                        # Combine Value and Context into a single HTML string with the tooltip
                        def create_display_cell(row):
                            val = str(row['Value']) if pd.notna(row['Value']) else ""
                            if not val or val == "nan":
                                return "-"
                            context = str(row['Context']).replace("'", "&#39;")
                            return f"<span class='tooltip-trigger'>{val}<span class='tooltiptext'><b>Context:</b><br>{context}</span></span>"
                            
                        report_data['Display_Cell'] = report_data.apply(create_display_cell, axis=1)
                        
                        # Create a unique column identifier combining RSSD and Period
                        report_data['Col_Key'] = report_data['RSSD'].astype(str) + "_" + report_data['Period'].astype(str)
                        
                        # PIVOT THE DATA using the composite key
                        pivot_df = pd.pivot_table(
                            report_data,
                            index=['Field ID', 'Definition'], 
                            columns='Col_Key', 
                            values='Display_Cell',
                            aggfunc='first'
                        ).reset_index()
                        
                        st.success(f"Successfully retrieved and merged records for {len(selected_rssds)} bank(s) across {len(selected_periods)} period(s).")
                        
                        # --- RENDER PDF TABS ---
                        st.markdown("### 📄 Official PDF Reports")
                        tabs = st.tabs([f"{rssd_to_name[rssd]} ({rssd})" for rssd in selected_rssds])
                        
                        for i, rssd in enumerate(selected_rssds):
                            with tabs[i]:
                                # Loop through periods to create collapsible sections for each PDF
                                for period in selected_periods:
                                    formatted_date = format_date_label(period)
                                    
                                    with st.expander(f"📁 View Report for {formatted_date}"):
                                        pdf_bytes, api_error = fetch_pdf_from_api(rssd, formatted_date)
                                        
                                        if pdf_bytes:
                                            pdf_state_key = f"show_pdf_{rssd}_{period}"
                                            if pdf_state_key not in st.session_state:
                                                st.session_state[pdf_state_key] = False
                                                
                                            spacer, col1, col2 = st.columns([6, 2, 2])
                                            
                                            with col1:
                                                st.download_button(
                                                    label="⬇️ Download PDF", 
                                                    data=pdf_bytes,
                                                    file_name=f"Call_Report_{rssd}_{formatted_date.replace('/', '-')}.pdf",
                                                    mime="application/pdf",
                                                    type="primary", 
                                                    use_container_width=True,
                                                    key=f"dl_{rssd}_{period}" 
                                                )
                                            with col2:
                                                if st.button("👁️ Toggle Viewer", use_container_width=True, type="secondary", key=f"tg_{rssd}_{period}"):
                                                    st.session_state[pdf_state_key] = not st.session_state[pdf_state_key]
                                            
                                            if st.session_state[pdf_state_key]:
                                                base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
                                                pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="500px" type="application/pdf" style="border: 1px solid #E0E0E0; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 5px; margin-top: 10px;"></iframe>'
                                                st.markdown(pdf_display, unsafe_allow_html=True)
                                        else:
                                            st.warning(f"🚫 **Not Available:** The official PDF for **{rssd_to_name[rssd]}** ({formatted_date}) has not been published.")
                                            
                                            if api_error and "not found" not in api_error.lower():
                                                st.error(f"🔧 Technical Details: {api_error}")
                        
                        st.divider()

                        # --- Build Custom Dynamic HTML Table ---
                        css = """
                        <style>
                        @import url('https://fonts.googleapis.com/css2?family=Source+Sans+Pro:wght@400;600&display=swap');
                        
                        :root {
                            --wm-green: #115740;
                            --wm-gold: #B9975B;
                            --border-color: #E0E0E0;
                        }
                        body {
                            font-family: 'Source Sans Pro', sans-serif;
                            margin: 0;
                            padding: 2px;
                        }
                        .table-container {
                            border: 2px solid var(--wm-green);
                            border-radius: 5px;
                            background-color: #FFFFFF;
                        }
                        .custom-table {
                            width: 100%;
                            table-layout: fixed; 
                            border-collapse: collapse;
                            color: #222222;
                        }
                        .custom-table th, .custom-table td {
                            border-bottom: 1px solid var(--border-color);
                            border-right: 1px solid rgba(0,0,0,0.05);
                            padding: 10px 12px;
                            text-align: left;
                            font-size: 0.95em;
                            word-wrap: break-word;
                            overflow-wrap: break-word;
                        }
                        .custom-table th {
                            background-color: var(--wm-green);
                            color: #FFFFFF;
                            position: sticky;
                            top: 0;
                            z-index: 2;
                            border-bottom: 3px solid var(--wm-gold);
                            font-weight: 600;
                            vertical-align: bottom;
                        }
                        .tooltip-trigger {
                            position: relative;
                            display: inline-block;
                            cursor: help;
                            color: var(--wm-green);
                            font-weight: 600;
                            border-bottom: 1px dotted var(--wm-gold);
                        }
                        .tooltip-trigger .tooltiptext {
                            visibility: hidden;
                            background-color: var(--wm-green);
                            color: #FFFFFF;
                            text-align: left;
                            border: 1px solid var(--wm-gold);
                            border-radius: 4px;
                            padding: 8px 12px;
                            position: absolute;
                            z-index: 3;
                            top: 130%; 
                            left: 0;
                            opacity: 0;
                            transition: opacity 0.15s ease-in-out;
                            font-size: 0.85em;
                            font-weight: normal; 
                            white-space: normal; 
                            width: max-content;
                            max-width: 250px; 
                            box-shadow: 0px 4px 8px rgba(0,0,0,0.2);
                            pointer-events: none; 
                        }
                        .tooltip-trigger:hover .tooltiptext {
                            visibility: visible;
                            opacity: 1;
                        }
                        </style>
                        """
                        
                        bg_colors = ["#F4F9F5", "#FFF9F0", "#F0F6FC", "#FCF0F5", "#FAF4FC", "#F0FCF9"]
                        
                        table_html = "<div class='table-container'><table class='custom-table'>"
                        
                        # Build Headers (Period First, then Bank)
                        table_html += "<thead><tr><th style='width: 15%;'>Field ID</th><th style='width: 35%;'>Definition</th>"
                        
                        # Pre-calculate column keys and colors for ultra-fast row iteration
                        col_props = [] 
                        
                        for period in selected_periods:
                            formatted_date = format_date_label(period)
                            for i, rssd in enumerate(selected_rssds):
                                bank_name = rssd_to_name[rssd]
                                color_match = bg_colors[i % len(bg_colors)]
                                col_key = f"{rssd}_{period}"
                                col_props.append((col_key, color_match))
                                
                                table_html += f"<th style='border-bottom: 3px solid {color_match};'>{bank_name}<br><span style='font-weight:normal; font-size: 0.85em; opacity: 0.8;'>ID: {rssd} | {formatted_date}</span></th>"
                        
                        table_html += "</tr></thead><tbody>"
                        
                        # FAST ROW RENDER: Convert to list of dicts instead of using .iterrows
                        records = pivot_df.to_dict('records')
                        html_rows = []
                        
                        for row in records:
                            field = str(row.get('Field ID', '-'))
                            definition = str(row.get('Definition', '-')) 
                            
                            row_str = f"<tr><td>{field}</td><td>{definition}</td>"
                            
                            for col_key, color in col_props:
                                cell_html = row.get(col_key, "-")
                                if pd.isna(cell_html) or str(cell_html) == "nan":
                                    cell_html = "-"
                                    
                                row_str += f"<td style='background-color: {color};'>{cell_html}</td>"
                                
                            row_str += "</tr>"
                            html_rows.append(row_str)
                            
                        table_html += "".join(html_rows) + "</tbody></table></div>"
                        
                        # Render the table as an iframe component to prevent Streamlit layout lag
                        st.components.v1.html(css + table_html, height=1000, scrolling=True)

                    else:
                        st.info("No records found for the selected bank(s) and period(s).")

except Exception as e:
    st.error(f"Database/Python Error: {e}")