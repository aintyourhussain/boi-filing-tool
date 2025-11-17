import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime, timedelta
import hashlib
import os

# -------------------------------------------------
# PAGE CONFIG & THEME
# -------------------------------------------------
st.set_page_config(page_title="BOI Filing Tool", layout="wide")

# Custom dark theme + unique tab/button styling
custom_css = """
<style>
/* Main background */
[data-testid="stAppViewContainer"] {
    background: radial-gradient(circle at top left, #111827, #020617);
    color: #e5e7eb;
}

/* Remove default header background */
[data-testid="stHeader"] {
    background: transparent;
}

/* Sidebar color */
[data-testid="stSidebar"] {
    background-color: #020617;
}

/* Title font tweak */
h1, h2, h3, h4 {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

/* Buttons */
.stButton button {
    background: linear-gradient(90deg, #6366f1, #ec4899);
    color: white;
    border-radius: 999px;
    border: none;
    padding: 0.35rem 1.4rem;
}
.stButton button:hover {
    filter: brightness(1.08);
}

/* Tabs (toolbar) */
.stTabs [data-baseweb="tab-list"] {
    gap: 0.35rem;
}
.stTabs [data-baseweb="tab"] {
    background-color: #020617;
    color: #e5e7eb;
    border-radius: 999px 999px 0 0;
    padding-top: 0.5rem;
    padding-bottom: 0.5rem;
    font-weight: 500;
    border: 1px solid #1f2933;
}
.stTabs [aria-selected="true"] {
    background-color: #4f46e5 !important;
    color: white !important;
    border-color: #6366f1 !important;
}

/* Metrics cards text */
[data-testid="stMetricValue"] {
    color: #e5e7eb;
}
[data-testid="stMetricLabel"] {
    color: #9ca3af;
}
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

st.title("📄 Business Filing Data Processor")

# ==========================
# AUTH CONFIG
# ==========================
USERS_DB_FILE = "users_db.csv"
PROTECTED_KEY = "BOI2025VIP"  # <<< change this to your secret signup key


# ==========================
# AUTH HELPERS
# ==========================

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def load_users():
    if not os.path.exists(USERS_DB_FILE):
        return pd.DataFrame(columns=["username", "password_hash", "expiry"])
    df = pd.read_csv(USERS_DB_FILE)
    for col in ["username", "password_hash", "expiry"]:
        if col not in df.columns:
            df[col] = ""
    return df


def save_users(df: pd.DataFrame):
    df.to_csv(USERS_DB_FILE, index=False)


def create_user(username: str, password: str):
    df = load_users()
    if (df["username"] == username).any():
        return False, "User already exists."

    pwd_hash = hash_password(password)
    expiry_date = (datetime.utcnow() + timedelta(days=30)).date().isoformat()

    new_row = pd.DataFrame([{
        "username": username,
        "password_hash": pwd_hash,
        "expiry": expiry_date
    }])

    df = pd.concat([df, new_row], ignore_index=True)
    save_users(df)
    return True, f"Account created. Valid until {expiry_date}"


def check_login(username: str, password: str):
    df = load_users()
    row = df[df["username"] == username]
    if row.empty:
        return False, "User not found."

    pwd_hash = hash_password(password)
    if row.iloc[0]["password_hash"] != pwd_hash:
        return False, "Incorrect password."

    expiry_str = str(row.iloc[0]["expiry"])
    try:
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    except Exception:
        return False, "Invalid expiry data. Contact admin."

    if expiry < datetime.utcnow().date():
        return False, f"Account expired on {expiry_str}."

    return True, f"Welcome back! Account valid until {expiry_str}."


def signup_page():
    st.subheader("🔑 Sign Up")

    username = st.text_input("Email / Username")
    password = st.text_input("Password", type="password")
    confirm = st.text_input("Confirm Password", type="password")
    key = st.text_input("Protected Key", type="password", help="Ask admin for signup key")

    if st.button("Create Account"):
        if not username or not password or not confirm or not key:
            st.error("All fields are required.")
            return

        if password != confirm:
            st.error("Passwords do not match.")
            return

        if key != PROTECTED_KEY:
            st.error("Invalid Protected Key.")
            return

        ok, msg = create_user(username, password)
        if ok:
            st.success(msg)
        else:
            st.error(msg)


def login_page():
    st.subheader("🔐 Login")

    username = st.text_input("Email / Username", key="login_user")
    password = st.text_input("Password", type="password", key="login_pass")

    if st.button("Login"):
        if not username or not password:
            st.error("Please enter both username and password.")
            return

        ok, msg = check_login(username, password)
        if ok:
            st.success(msg)
            st.session_state["auth"] = True
            st.session_state["user"] = username
        else:
            st.error(msg)


def logout():
    st.session_state["auth"] = False
    st.session_state["user"] = None
    st.success("Logged out.")


# =====================================================
# Helper: Row selection for combiner
# =====================================================

def select_rows(df: pd.DataFrame, choice: str) -> pd.DataFrame:
    """
    Parse user row selection string and return subset of df.

    Supported:
      - ALL
      - first 100
      - last 100
      - 34-134  (1-based inclusive range)
    """
    if not choice:
        return df

    choice = choice.strip().lower()

    if choice == "all":
        return df

    if choice.startswith("first"):
        try:
            n = int(choice.split()[1])
            return df.head(n)
        except Exception:
            return df

    if choice.startswith("last"):
        try:
            n = int(choice.split()[1])
            return df.tail(n)
        except Exception:
            return df

    if "-" in choice:
        try:
            start, end = choice.split("-")
            start = int(start)
            end = int(end)
            return df.iloc[start - 1:end]
        except Exception:
            return df

    return df


# =====================================================
# FLORIDA backend
# =====================================================

def process_florida(file_bytes: bytes, exact_date_str: str, mailing_only: bool) -> pd.DataFrame:
    """Process Florida TXT and return a DataFrame.
       If mailing_only=True, output is in standard format:
       Name | Address | City | State | Zipcode | Filing Date | Document Number
    """

    def split_parts(line: str):
        return [p for p in re.split(r"\s{3,}|\t+", line.rstrip("\n\r")) if p.strip()]

    def parse_entity_and_name(first_part: str):
        m = re.match(r"^([A-Z]\d{11})(.*)$", first_part.strip())
        if not m:
            return "", ""
        return m.group(1).strip(), m.group(2).strip()

    def extract_principal(parts):
        if len(parts) < 5:
            return "", "", ""
        p_street = parts[2].strip()
        p_city   = parts[3].strip().rstrip(",")
        m_zip = re.search(r"(\d{5})", parts[4])
        p_zip = m_zip.group(1) if m_zip else ""
        return p_street, p_city, p_zip

    def extract_mailing(parts):
        if len(parts) < 8:
            return "", "", "", ""
        m_street = parts[5].strip()
        m_city   = parts[6].strip().rstrip(",")
        state_token = parts[7].strip()
        m_statezip = re.search(r"([A-Z]{2})\s*(\d{5})", state_token)
        if m_statezip:
            m_state = m_statezip.group(1)
            m_zip   = m_statezip.group(2)
        else:
            m_state, m_zip = "", ""
        return m_street, m_city, m_state, m_zip

    def extract_filing_date(parts):
        for p in parts[8:]:
            m = re.search(r"(\d{8})", p)
            if m:
                mm, dd, yyyy = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:]
                return f"{mm}/{dd}/{yyyy}"
        return ""

    text = file_bytes.decode("utf-8", errors="ignore")
    rows = []

    for raw in text.splitlines():
        line = raw.replace("\x00", " ").rstrip("\n\r")
        if not line.strip():
            continue

        if not re.match(r"^[A-Z]\d{11}", line):
            continue

        parts = split_parts(line)
        if len(parts) < 8:
            continue

        entity_id, business_name = parse_entity_and_name(parts[0])
        if not entity_id:
            continue

        p_street, p_city, p_zip = extract_principal(parts)
        m_street, m_city, m_state, m_zip = extract_mailing(parts)
        filing_date = extract_filing_date(parts)

        rows.append([
            entity_id, business_name, filing_date,
            p_street, p_city, p_zip,
            m_street, m_city, m_state, m_zip
        ])

    df = pd.DataFrame(rows, columns=[
        "Entity ID", "Business Name", "Filing Date",
        "Principal Street", "Principal City", "Principal ZIP",
        "Mailing Street", "Mailing City", "Mailing State", "Mailing ZIP"
    ])

    df["Filing Date Parsed"] = pd.to_datetime(df["Filing Date"], format="%m/%d/%Y", errors="coerce")

    if exact_date_str:
        try:
            exact_date = pd.to_datetime(exact_date_str, format="%m/%d/%Y")
            df = df[df["Filing Date Parsed"] == exact_date]
        except Exception:
            pass

    df = df.drop(columns=["Filing Date Parsed"], errors="ignore")

    if mailing_only:
        mailing_df = df[[
            "Business Name",
            "Mailing Street",
            "Mailing City",
            "Mailing State",
            "Mailing ZIP",
            "Filing Date",
            "Entity ID"
        ]].copy()

        mailing_df.columns = [
            "Name",
            "Address",
            "City",
            "State",
            "Zipcode",
            "Filing Date",
            "Document Number"
        ]
        df = mailing_df
    else:
        df = df[[
            "Entity ID", "Business Name", "Filing Date",
            "Principal Street", "Principal City", "Principal ZIP",
            "Mailing Street", "Mailing City", "Mailing State", "Mailing ZIP"
        ]]

    df = df.replace(r"^\s*$", pd.NA, regex=True)
    df = df.dropna(how="any")

    return df


# =====================================================
# WASHINGTON backend
# =====================================================

def process_washington_streamlit(file, added_date: str) -> pd.DataFrame:
    """
    Washington CSV → standard format:
    Name | Address | City | State | Zipcode | Filing Date | Document Number
    """

    def split_address(addr: str):
        if pd.isna(addr):
            return pd.Series(["", "", "", ""])
        addr_str = str(addr).strip()
        parts = [p.strip() for p in addr_str.split(",") if p.strip()]

        street = parts[0] if len(parts) > 0 else ""
        city   = parts[1] if len(parts) > 1 else ""
        state  = parts[2] if len(parts) > 2 else ""

        zip_raw = parts[3] if len(parts) > 3 else ""
        m = re.search(r"\d{5}", zip_raw)
        zipcode = m.group(0) if m else ""

        return pd.Series([street, city, state, zipcode])

    data = pd.read_csv(file)
    data.columns = data.columns.str.strip()
    data["Filing Date"] = added_date

    filtered = data[
        (data["Status"] == "Active") &
        (data["Principal Office Address"].notna()) &
        (data["Business Type"].str.strip().str.upper() == "WA LIMITED LIABILITY COMPANY")
    ].copy()

    addr_df = filtered["Principal Office Address"].apply(split_address)
    addr_df.columns = ["Address", "City", "State", "Zipcode"]

    filtered = pd.concat([filtered, addr_df], axis=1)

    drop_cols = [
        "Nonprofit EIN", "Status",
        "Registered Agent Name", "Business Type",
        "Principal Office Address"
    ]
    filtered = filtered.drop(columns=drop_cols, errors="ignore")

    final = pd.DataFrame()
    final["Name"] = filtered["Business Name"].astype(str).str.strip()
    final["Address"] = filtered["Address"].astype(str).str.strip()
    final["City"] = filtered["City"].astype(str).str.strip()
    final["State"] = filtered["State"].astype(str).str.strip()
    final["Zipcode"] = filtered["Zipcode"].astype(str).str.strip()
    final["Filing Date"] = filtered["Filing Date"].astype(str).str.strip()
    final["Document Number"] = filtered["UBI#"].astype(str).str.strip()

    final = final.replace(r"^\s*$", pd.NA, regex=True)
    final = final.dropna(how="any")

    return final


# =====================================================
# WEST VIRGINIA backend
# =====================================================

def process_wv_streamlit(file) -> pd.DataFrame:
    """
    WV CSV → standard format:
    Name | Address | City | State | Zipcode | Filing Date | Document Number
    """

    df = pd.read_csv(file)
    df.columns = df.columns.str.strip()

    df["Filing Date"] = pd.to_datetime(
        df["Effective Date"], errors="coerce"
    ).dt.strftime("%m/%d/%Y")

    street1 = df["Street1"].fillna("").astype(str).str.strip()
    street2 = df["Street2"].fillna("").astype(str).str.strip()
    address = street1.where(street2 == "", street1 + ", " + street2)

    zip5 = df["ZipCode"].astype(str).str.extract(r"(\d{5})", expand=False)

    mask = (
        df["Organization Name"].notna() &
        df["Street1"].notna() &
        df["City"].notna() &
        df["StateProvince"].notna() &
        df["ZipCode"].notna() &
        df["Termination Date"].isna()
    )

    df_f = df[mask].copy()

    final = pd.DataFrame()
    final["Name"] = df_f["Organization Name"].astype(str).str.strip()
    final["Address"] = address.loc[df_f.index].astype(str).str.strip()
    final["City"] = df_f["City"].astype(str).str.strip()
    final["State"] = df_f["StateProvince"].astype(str).str.strip()
    final["Zipcode"] = zip5.loc[df_f.index].astype(str).str.strip()
    final["Filing Date"] = df_f["Filing Date"].astype(str).str.strip()
    final["Document Number"] = df_f["Id"].astype(str).str.strip()

    final = final.replace(r"^\s*$", pd.NA, regex=True)
    final = final.dropna(how="any")

    return final


# =====================================================
# Combiner page
# =====================================================

def combiner_page():
    st.header("🔗 Combine Files")

    st.write("All input files must be in this exact format:")
    st.code("Name | Address | City | State | Zipcode | Filing Date | Document Number", language="text")

    uploaded_files = st.file_uploader(
        "Upload one or more processed Excel files",
        type=["xlsx"],
        accept_multiple_files=True
    )

    if not uploaded_files:
        return

    st.write("For each file, specify how many rows you want (ALL, first 100, last 50, 34-134, etc.)")

    required_cols = ["Name", "Address", "City", "State", "Zipcode", "Filing Date", "Document Number"]
    selections = {}
    valid_files = []

    for i, file in enumerate(uploaded_files):
        with st.expander(f"{file.name}"):
            file.seek(0)
            df_preview = pd.read_excel(file, engine="openpyxl")
            missing = [c for c in required_cols if c not in df_preview.columns]

            if missing:
                st.error(f"❌ Missing columns: {missing} — this file will be skipped.")
            else:
                st.write(df_preview.head())
                key = f"rows_{i}"
                selections[file.name] = st.text_input(
                    f"Rows to take from {file.name}",
                    value="ALL",
                    key=key
                )
                valid_files.append(file.name)

    if st.button("🔗 Combine Selected Rows"):
        all_frames = []

        for file in uploaded_files:
            if file.name not in valid_files:
                continue

            file.seek(0)
            df = pd.read_excel(file, engine="openpyxl")
            choice = selections.get(file.name, "ALL")
            df_sel = select_rows(df, choice)
            all_frames.append(df_sel)

        if not all_frames:
            st.error("❌ No valid data to combine (all files had missing columns).")
            return

        combined = pd.concat(all_frames, ignore_index=True)
        st.success(f"✅ Combined rows: {len(combined):,}")

        out = BytesIO()
        combined.to_excel(out, index=False)
        st.download_button(
            "⬇️ Download Combined Excel",
            data=out.getvalue(),
            file_name="Combined_States.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


# =====================================================
# State processing page
# =====================================================

def state_page():
    st.header("🏛 Process Individual State Files")

    state = st.selectbox("Select State", ["Florida", "Washington", "West Virginia"])

    if state == "Florida":
        uploaded = st.file_uploader("Upload Florida TXT file", type=["txt"])
        exact_date = st.text_input("Exact Filing Date filter (MM/DD/YYYY) or leave blank")
        mailing_only = st.checkbox("Output in Mailing-Only standard format?", value=True)

        if uploaded and st.button("Process Florida"):
            df = process_florida(uploaded.read(), exact_date, mailing_only)
            st.success(f"Rows after processing: {len(df):,}")
            st.dataframe(df.head())

            out = BytesIO()
            df.to_excel(out, index=False)
            st.download_button(
                "⬇️ Download Florida Excel",
                data=out.getvalue(),
                file_name="Florida_Output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

    elif state == "Washington":
        uploaded = st.file_uploader("Upload Washington CSV file", type=["csv"])
        added_date = st.text_input("Filing Date to ADD to each row (MM/DD/YYYY)")

        if uploaded and added_date and st.button("Process Washington"):
            df = process_washington_streamlit(uploaded, added_date)
            st.success(f"Rows after processing: {len(df):,}")
            st.dataframe(df.head())

            out = BytesIO()
            df.to_excel(out, index=False)
            st.download_button(
                "⬇️ Download Washington Excel",
                data=out.getvalue(),
                file_name="Washington_Clean.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

    else:  # West Virginia
        uploaded = st.file_uploader("Upload West Virginia CSV file", type=["csv"])

        if uploaded and st.button("Process West Virginia"):
            df = process_wv_streamlit(uploaded)
            st.success(f"Rows after processing: {len(df):,}")
            st.dataframe(df.head())

            out = BytesIO()
            df.to_excel(out, index=False)
            st.download_button(
                "⬇️ Download WV Excel",
                data=out.getvalue(),
                file_name="WV_Output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )


# =====================================================
# MAIN APP WITH AUTH + TOP TABS
# =====================================================

if "auth" not in st.session_state:
    st.session_state["auth"] = False
if "user" not in st.session_state:
    st.session_state["user"] = None

if not st.session_state["auth"]:
    choice = st.sidebar.radio("Authentication", ["Login", "Sign Up"])
    if choice == "Login":
        login_page()
    else:
        signup_page()
    st.stop()

# Logged-in view
st.sidebar.markdown(f"**Logged in as:** {st.session_state['user']}")
if st.sidebar.button("Logout"):
    logout()
    st.stop()

# Top toolbar tabs
tab_home, tab_process, tab_combine = st.tabs(
    ["🏠 Home", "🏛 Process State Files", "🔗 Combine Files"]
)

with tab_home:
    st.subheader("Dashboard")
    st.markdown(
        """
        Welcome to the **BOI Filing Multi-State Processor**.

        - Use **Process State Files** to clean and format data for each state  
        - Use **Combine Files** to merge all processed files into one master sheet  
        """
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("States Supported", "3", help="Florida, Washington, West Virginia")
    with col2:
        st.metric("Standard Columns", "7", help="Name, Address, City, State, Zip, Filing Date, Doc #")
    with col3:
        st.metric("Account Validity", "30 days", help="Per signup with protected key")

with tab_process:
    state_page()

with tab_combine:
    combiner_page()
    # ---------------------------
# FOOTER
# ---------------------------
footer_css = """
<style>
.footer-text {
    position: fixed;
    bottom: 10px;
    right: 20px;
    color: #9ca3af;
    font-size: 14px;
    font-family: 'Segoe UI', sans-serif;
    z-index: 9999;
}
</style>
<div class="footer-text">
    Created by Hussain — the pro coder
</div>
"""

st.markdown(footer_css, unsafe_allow_html=True)


