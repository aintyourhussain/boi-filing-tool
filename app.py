import streamlit as st
import pandas as pd
import re
from io import BytesIO

st.set_page_config(page_title="BOI Filing Tool", layout="wide")

st.title("📄 BOI Filing Multi-State Processor")


# ================================
# Helpers
# ================================

def select_rows(df: pd.DataFrame, choice: str) -> pd.DataFrame:
    """Parse user row selection string and return subset of df."""
    if not choice:
        return df

    choice = choice.strip().lower()

    if choice == "all":
        return df

    # first N
    if choice.startswith("first"):
        parts = choice.split()
        if len(parts) >= 2 and parts[1].isdigit():
            n = int(parts[1])
            return df.head(n)

    # last N
    if choice.startswith("last"):
        parts = choice.split()
        if len(parts) >= 2 and parts[1].isdigit():
            n = int(parts[1])
            return df.tail(n)

    # range A-B
    if "-" in choice:
        try:
            a, b = choice.split("-", 1)
            start = int(a)
            end = int(b)
            if start < 1:
                start = 1
            if end > len(df):
                end = len(df)
            if start <= end:
                # user uses 1-based indexing
                return df.iloc[start - 1:end]
        except Exception:
            pass

    # fallback: all rows
    return df


# ================================
# Florida backend
# ================================

def process_florida(file_bytes: bytes, date_filter: str, mailing_only: bool) -> pd.DataFrame:
    text = file_bytes.decode("utf-8", errors="ignore")
    rows = []

    for raw in text.splitlines():
        line = raw.replace("\x00", " ").rstrip("\n\r")
        if not line.strip():
            continue

        # Florida doc number pattern: 1 letter + 11 digits at start
        if not re.match(r"^[A-Z]\d{11}", line):
            continue

        # split into parts using 3+ spaces or tabs
        parts = [p for p in re.split(r"\s{3,}|\t+", line) if p.strip()]
        if len(parts) < 8:
            continue

        # Entity ID + Business Name from first part
        m = re.match(r"^([A-Z]\d{11})(.*)$", parts[0].strip())
        if not m:
            continue
        entity_id = m.group(1).strip()
        name = m.group(2).strip()

        # Principal address block (based on your format)
        p_street = parts[2].strip() if len(parts) > 2 else ""
        p_city = parts[3].strip().rstrip(",") if len(parts) > 3 else ""
        p_zip_match = re.search(r"(\d{5})", parts[4]) if len(parts) > 4 else None
        p_zip = p_zip_match.group(1) if p_zip_match else ""

        # Mailing address block
        m_street = parts[5].strip() if len(parts) > 5 else ""
        m_city = parts[6].strip().rstrip(",") if len(parts) > 6 else ""
        state_token = parts[7].strip() if len(parts) > 7 else ""
        m_statezip = re.search(r"([A-Z]{2})\s*(\d{5})", state_token)
        if m_statezip:
            m_state = m_statezip.group(1)
            m_zip = m_statezip.group(2)
        else:
            m_state, m_zip = "", ""

        # Filing Date: first 8-digit block after address parts
        filing_date = ""
        for p in parts[8:]:
            m_date = re.search(r"(\d{8})", p)
            if m_date:
                digits = m_date.group(1)
                mm, dd, yyyy = digits[0:2], digits[2:4], digits[4:]
                filing_date = f"{mm}/{dd}/{yyyy}"
                break

        rows.append([
            entity_id, name, filing_date,
            p_street, p_city, p_zip,
            m_street, m_city, m_state, m_zip
        ])

    df = pd.DataFrame(rows, columns=[
        "Entity ID", "Name", "Filing Date",
        "Principal Street", "Principal City", "Principal ZIP",
        "Mailing Street", "Mailing City", "Mailing State", "Mailing ZIP"
    ])

    # optional exact date filter
    if date_filter:
        df["Filing Date Parsed"] = pd.to_datetime(df["Filing Date"], format="%m/%d/%Y", errors="coerce")
        try:
            target = pd.to_datetime(date_filter, format="%m/%d/%Y")
            df = df[df["Filing Date Parsed"] == target]
        except Exception:
            pass
        df = df.drop(columns=["Filing Date Parsed"], errors="ignore")

    # format to master schema if mailing_only
    if mailing_only:
        df = df[[
            "Name",
            "Mailing Street",
            "Mailing City",
            "Mailing State",
            "Mailing ZIP",
            "Filing Date",
            "Entity ID"
        ]].copy()
        df.columns = [
            "Name",
            "Address",
            "City",
            "State",
            "Zipcode",
            "Filing Date",
            "Document Number"
        ]

    return df.replace(r"^\s*$", pd.NA, regex=True).dropna(how="any")


# ================================
# Washington backend
# ================================

def process_washington(file, added_date: str) -> pd.DataFrame:
    df = pd.read_csv(file)

    # ensure clean col names
    df.columns = df.columns.str.strip()

    df["Added Filing Date"] = added_date

    mask = (
        (df["Status"] == "Active") &
        df["Principal Office Address"].notna() &
        (df["Business Type"].str.strip().str.upper() == "WA LIMITED LIABILITY COMPANY")
    )
    df = df[mask].copy()

    addr = df["Principal Office Address"].fillna("").astype(str)

    # split address: street, city, state, zip, country (if present)
    parts = addr.str.split(",", expand=True)

    street = parts[0].str.strip()
    city = parts[1].str.strip() if parts.shape[1] > 1 else ""
    state = ""
    zipcode = ""

    if parts.shape[1] > 2:
        state_zip_raw = parts[2].fillna("").astype(str) + "," + (
            parts[3].fillna("").astype(str) if parts.shape[1] > 3 else ""
        )
        # extract state and zip from "WA, 98155-6305" or "WA 98155"
        state = state_zip_raw.str.extract(r"([A-Z]{2})", expand=False)
        zipcode = state_zip_raw.str.extract(r"(\d{5})", expand=False)

    final = pd.DataFrame()
    final["Name"] = df["Business Name"].astype(str).str.strip()
    final["Address"] = street
    final["City"] = city
    final["State"] = state
    final["Zipcode"] = zipcode
    final["Filing Date"] = df["Added Filing Date"].astype(str).str.strip()
    final["Document Number"] = df["UBI"].astype(str).str.strip()

    final = final.replace(r"^\s*$", pd.NA, regex=True).dropna(how="any")
    return final


# ================================
# West Virginia backend
# ================================

def process_wv(file) -> pd.DataFrame:
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

    final = final.replace(r"^\s*$", pd.NA, regex=True).dropna(how="any")
    return final


# ================================
# File combiner page
# ================================

def combiner_page():
    st.header("📌 Combine Processed State Files")

    st.write("All input files must already be in this format:")
    st.code("Name | Address | City | State | Zipcode | Filing Date | Document Number", language="text")

    uploaded_files = st.file_uploader(
        "Upload one or more processed Excel files",
        type=["xlsx"],
        accept_multiple_files=True
    )

    if not uploaded_files:
        return

    st.write("For each file, specify how many rows you want:")
    st.write("- `ALL`")
    st.write("- `first 100`")
    st.write("- `last 50`")
    st.write("- `34-134` (range, 1-based)")

    selections = {}
    for i, file in enumerate(uploaded_files):
        with st.expander(f"{file.name}"):
            df_preview = pd.read_excel(file, engine="openpyxl")
            st.write(df_preview.head())
            key = f"rows_{i}"
            default = "ALL"
            selections[file.name] = st.text_input(
                f"Rows to take from {file.name}",
                value=default,
                key=key
            )

    if st.button("🔗 Combine Selected Rows"):
        all_frames = []
        for i, file in enumerate(uploaded_files):
            file.seek(0)
            df = pd.read_excel(file, engine="openpyxl")
            rows_choice = selections.get(file.name, "ALL")
            df_sel = select_rows(df, rows_choice)
            all_frames.append(df_sel)

        combined = pd.concat(all_frames, ignore_index=True)
        st.success(f"Combined rows: {len(combined):,}")

        out = BytesIO()
        combined.to_excel(out, index=False)
        st.download_button(
            "⬇️ Download Combined Excel",
            data=out.getvalue(),
            file_name="Combined_States.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


# ================================
# State processing page
# ================================

def state_page():
    st.header("🏛 Process Individual State Files")

    state = st.selectbox("Select State", ["Florida", "Washington", "West Virginia"])

    if state == "Florida":
        uploaded = st.file_uploader("Upload Florida TXT file", type=["txt"])
        date_filter = st.text_input("Exact Filing Date filter (MM/DD/YYYY) or leave blank")
        mailing_only = st.checkbox("Output in Mailing-Only standard format?", value=True)

        if uploaded and st.button("Process Florida"):
            df = process_florida(uploaded.read(), date_filter, mailing_only)
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
            df = process_washington(uploaded, added_date)
            st.success(f"Rows after processing: {len(df):,}")
            st.dataframe(df.head())

            out = BytesIO()
            df.to_excel(out, index=False)
            st.download_button(
                "⬇️ Download Washington Excel",
                data=out.getvalue(),
                file_name="Washington_Output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

    else:  # West Virginia
        uploaded = st.file_uploader("Upload West Virginia CSV file", type=["csv"])

        if uploaded and st.button("Process West Virginia"):
            df = process_wv(uploaded)
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


# ================================
# Main navigation
# ================================

page = st.sidebar.radio("Navigate", ["Home", "Process State Files", "Combine Files"])

if page == "Home":
    st.subheader("Welcome, Hussain 👋")
    st.markdown(
        """
        This app helps you process **state-level business filing data** and convert it into a
        unified format for BOI letters.

        1. Use **Process State Files** to generate per-state Excel files  
        2. Then go to **Combine Files** to merge them into a single master file  
        """
    )

elif page == "Process State Files":
    state_page()

else:
    combiner_page()
