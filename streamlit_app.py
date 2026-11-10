import os
import re
from datetime import date
from urllib.parse import quote, urlencode

import pandas as pd
import requests
import streamlit as st

CENSUS_BASE_URL = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
REQUEST_TIMEOUT = 60
MAX_RANGE_MONTHS = 36
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

CENSUS_VARIABLES = [
    "I_COMMODITY",
    "I_COMMODITY_SDESC",
    "GEN_VAL_MO",
    "GEN_CIF_MO",
    "GEN_QY1_MO",
    "UNIT_QY1",
    "CTY_CODE",
    "CTY_NAME",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def months_between(start: str, end: str) -> int:
    return (int(end[:4]) - int(start[:4])) * 12 + (int(end[5:7]) - int(start[5:7])) + 1


def month_options(first_year: int = 2013) -> list:
    """YYYY-MM strings from first_year up to two months ago (Census lags ~6 weeks)."""
    today = date.today()
    last_idx = today.year * 12 + (today.month - 1) - 2
    return [f"{i // 12}-{i % 12 + 1:02d}" for i in range(first_year * 12, last_idx + 1)]


def validate_hs(hs_code: str):
    hs_clean = hs_code.strip().rstrip("*")
    if not hs_clean.isdigit():
        raise ValueError("HS code must contain digits only.")
    if not hs_clean.startswith("76"):
        raise ValueError("Aluminum commodities fall under HS Chapter 76 (code must start with 76).")
    if len(hs_clean) not in (2, 4, 6, 10):
        raise ValueError("HS code must be 2, 4, 6 or 10 digits long (e.g., 76, 7601, 760110).")
    return hs_clean, ("HS10" if len(hs_clean) == 10 else "HS6")


def get_api_key(typed_key: str) -> str:
    if typed_key.strip():
        return typed_key.strip()
    try:
        secret = st.secrets.get("CENSUS_API_KEY", "")
    except Exception:
        secret = ""
    return (secret or os.getenv("CENSUS_API_KEY") or "").strip()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_census(hs_clean: str, level: str, start: str, end: str, key: str) -> pd.DataFrame:
    params = {
        "get": ",".join(CENSUS_VARIABLES),
        "time": f"from {start} to {end}",
        "I_COMMODITY": f"{hs_clean}*",
        "COMM_LVL": level,
    }
    if key:
        params["key"] = key

    # Keep * and commas literal, spaces as %20 (as Census documents).
    url = f"{CENSUS_BASE_URL}?{urlencode(params, quote_via=quote, safe='*,')}"

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach the Census API: {str(exc).replace(key, '***') if key else exc}")

    if response.status_code == 204:
        return pd.DataFrame()
    if response.status_code != 200:
        body = response.text[:500].replace(key, "***") if key else response.text[:500]
        raise RuntimeError(f"Census API returned HTTP {response.status_code}: {body}")

    try:
        data = response.json()
    except ValueError:
        body = response.text[:300]
        if "invalid key" in body.lower():
            raise RuntimeError("Census says the API key is invalid. Check that you activated it from the email.")
        if not key:
            raise RuntimeError(
                "No API key was sent. Add one in the sidebar or in Streamlit secrets "
                "(free key: https://api.census.gov/data/key_signup.html)."
            )
        raise RuntimeError(f"Census returned a non-JSON response: {body}")

    if not data or len(data) < 2:
        return pd.DataFrame()

    df = pd.DataFrame(data[1:], columns=data[0])
    # Census echoes predicate fields back, so I_COMMODITY can appear twice.
    return df.loc[:, ~df.columns.duplicated()]


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Exportwerk Aluminum Census Explorer", layout="wide")
st.title("Exportwerk Aluminum Pricing Benchmark Engine")
st.caption("Raw US import rows for aluminum (HS Chapter 76), from US Census Bureau monthly trade data.")

with st.sidebar:
    st.header("Settings")
    typed_key = st.text_input(
        "Census API key (optional)",
        type="password",
        help="Leave blank if the key is set in Streamlit secrets as CENSUS_API_KEY.",
    )

months = month_options()
col1, col2, col3 = st.columns(3)
hs_code = col1.text_input("HS code", value="760110", help="e.g. 7601, 7602, 7606, 7612, 760110")
start_period = col2.selectbox("Start month", months, index=max(len(months) - 12, 0))
end_period = col3.selectbox("End month", months, index=len(months) - 1)

if st.button("Fetch data", type="primary"):
    try:
        hs_clean, level = validate_hs(hs_code)
        if start_period > end_period:
            raise ValueError("Start month must not be after end month.")
        if months_between(start_period, end_period) > MAX_RANGE_MONTHS:
            raise ValueError(f"Range too long: please request at most {MAX_RANGE_MONTHS} months.")

        with st.spinner("Querying the Census API..."):
            df = fetch_census(hs_clean, level, start_period, end_period, get_api_key(typed_key))

        if df.empty:
            st.warning(f"No trade records found for HS code {hs_clean} between {start_period} and {end_period}.")
        else:
            st.session_state["result"] = (df, f"census_raw_{hs_clean}_{start_period}_to_{end_period}.csv")
    except (ValueError, RuntimeError) as exc:
        st.session_state.pop("result", None)
        st.error(str(exc))

if "result" in st.session_state:
    df, filename = st.session_state["result"]
    st.success(f"{len(df):,} rows returned.")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "Download CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
    )