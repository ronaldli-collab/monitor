"""
OIF Monitor (Streamlit)
=======================

Streamlit port of oif_monitor_gui.py, designed to be hosted on
PythonAnywhere (or anywhere else that can run a long-lived Streamlit
process). Same data pipeline (get_all_oif_data -> build_oif_df ->
build_monitor_table) as the Tkinter version, but rendered as a web
dashboard with:

    - A "Refresh now" button for an immediate manual pull
    - An "Auto-refresh" toggle + interval control, using
      streamlit_autorefresh (falls back to manual-only if that
      package isn't installed)
    - A status line showing last-updated time or the last error
    - A grid of Status x Day-bucket cells, each with an OIF count and
      an expander you can open to see the full OIF list

Run locally:   streamlit run oif_monitor_streamlit.py
Deploy notes:  see the bottom of this file / the accompanying README
               for PythonAnywhere-specific instructions.

Token handling: instead of a TOKEN.py file (which doesn't fit a web
deployment well), this reads the token in this order:
    1. st.secrets["TOKEN"]           (recommended - .streamlit/secrets.toml)
    2. environment variable OIF_TOKEN
    3. a manual text-input box in the sidebar (session-only, not saved)
"""

import os
import time
from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    HAVE_AUTOREFRESH = True
except ImportError:
    HAVE_AUTOREFRESH = False


# ============================================================
# CONFIG (unchanged from oif_monitor.py / oif_monitor_gui.py)
# ============================================================

PACKING_SLIPS_URL = "https://www.solarbrain.com.au/api/v1/operation/packing_slips"

STATUSES = ["Open", "Scanned", "Picking"]

SHEET_COLUMNS = ["OIF", "OSW", "Shipping Method", "ETP", "Status", "Remarks"]

DAY_BUCKETS = ["Same Day", "1 Day", ">2 Days"]

DEFAULT_REFRESH_SECONDS = 60


# ============================================================
# Data pipeline (unchanged logic)
# ============================================================

def _format_etp(value):
    """Format an etp timestamp as 'YYYY-MM-DD' in the offset it was given in."""
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""

    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""

    return parsed.strftime("%Y-%m-%d")


def get_all_oif_data(token, statuses=None, per_page=100, timeout=30, progress=None):
    """
    Pull raw packing-slip records for each status, looping through every
    page (no district_id / shipping_method_id filter).

    `progress`, if given, is called as progress(message) so the UI can
    show what page/status is currently being pulled.
    """
    if statuses is None:
        statuses = STATUSES

    headers = {"token": token, "accept": "application/json"}
    cookies = {"_osw_session": token}

    records = []

    for status in statuses:
        page = 1

        while True:
            params = [
                ("status", status),
                ("sort_fields[]", '{"field":"manual_etp","by":"asc"}'),
                ("sort_fields[]", '{"field":"scan_at","by":"asc"}'),
                ("page", page),
                ("per_page", per_page),
            ]

            response = requests.get(
                PACKING_SLIPS_URL,
                params=params,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()

            body = data.get("response", {}).get("body", [])
            records.extend(body)

            meta = data.get("response", {}).get("meta", {})
            total_page = meta.get("total_page", 1)

            if progress:
                progress(f"{status}: page {page}/{total_page} ({len(body)} records)")

            if page >= total_page:
                break
            page += 1

    return records


def build_oif_df(records):
    """Extract the fields we care about into a DataFrame with an empty Remarks col."""
    rows = []
    for item in records:
        rows.append(
            {
                "OIF": item.get("tran_id") or "",
                "OSW": item.get("sales_order_number") or "",
                "Shipping Method": item.get("shipping_method") or "",
                "ETP": _format_etp(item.get("etp")),
                "Status": item.get("status") or "",
                "Remarks": "",
            }
        )

    return pd.DataFrame(rows, columns=SHEET_COLUMNS)


def get_day_bucket(etp_str, today=None):
    """Map a 'YYYY-MM-DD' ETP string to a DAY_BUCKETS entry relative to `today`."""
    if not etp_str:
        return None
    if today is None:
        today = date.today()

    try:
        etp_date = datetime.strptime(etp_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

    delta = (etp_date - today).days

    if delta <= 0:
        return "Same Day"
    elif delta == 1:
        return "1 Day"
    else:
        return ">2 Days"


def build_monitor_table(df, today=None):
    """Turn a build_oif_df() DataFrame into {status: {bucket: [OIF, ...]}}."""
    today = today or date.today()
    table = {status: {bucket: [] for bucket in DAY_BUCKETS} for status in STATUSES}

    for _, row in df.iterrows():
        status = row["Status"]
        bucket = get_day_bucket(row["ETP"], today)
        if status in table and bucket in DAY_BUCKETS:
            table[status][bucket].append(row["OIF"])

    return table


# ============================================================
# Token resolution
# ============================================================

def resolve_token():
    token = st.secrets.get("TOKEN") if hasattr(st, "secrets") else None
    if not token:
        token = os.environ.get("OIF_TOKEN")
    return token


# ============================================================
# Streamlit app
# ============================================================

st.set_page_config(page_title="OIF Monitor", layout="wide")

# ---- session state defaults ----
st.session_state.setdefault("latest_table", None)
st.session_state.setdefault("status_msg", "Not fetched yet.")
st.session_state.setdefault("auto_refresh", True)
st.session_state.setdefault("interval_seconds", DEFAULT_REFRESH_SECONDS)

st.title("OIF Monitor")

token = resolve_token()
with st.sidebar:
    st.header("Settings")
    if not token:
        st.warning(
            "No token found in st.secrets['TOKEN'] or the OIF_TOKEN "
            "environment variable. Enter one below for this session only."
        )
        token = st.text_input("API token", type="password")

    st.session_state.auto_refresh = st.checkbox(
        "Auto-refresh", value=st.session_state.auto_refresh
    )
    st.session_state.interval_seconds = st.number_input(
        "Refresh interval (seconds)",
        min_value=10,
        max_value=3600,
        step=10,
        value=st.session_state.interval_seconds,
    )
    refresh_clicked = st.button("Refresh now", use_container_width=True)

    if HAVE_AUTOREFRESH:
        st.caption("Auto-refresh is active via streamlit-autorefresh.")
    else:
        st.caption(
            "streamlit-autorefresh isn't installed, so auto-refresh only "
            "happens while you're interacting with the page (or add "
            "`streamlit-autorefresh` to requirements.txt for true "
            "background polling)."
        )

status_placeholder = st.empty()
status_placeholder.caption(st.session_state.status_msg)


def fetch_and_store():
    if not token:
        st.session_state.status_msg = "Error: no API token configured."
        return

    progress_box = status_placeholder.container()
    try:
        def progress(msg):
            progress_box.caption(msg)

        records = get_all_oif_data(token, statuses=STATUSES, progress=progress)
        df = build_oif_df(records)
        table = build_monitor_table(df)
        st.session_state.latest_table = table
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state.status_msg = f"Last updated: {ts}"
    except Exception as exc:
        st.session_state.status_msg = f"Error: {exc}"


# ---- decide whether to fetch this run ----
if HAVE_AUTOREFRESH and st.session_state.auto_refresh:
    st_autorefresh(
        interval=int(st.session_state.interval_seconds) * 1000,
        key="oif_autorefresh",
    )

should_fetch = (
    refresh_clicked
    or st.session_state.latest_table is None
    or (HAVE_AUTOREFRESH and st.session_state.auto_refresh)
)

if should_fetch:
    fetch_and_store()

status_placeholder.caption(st.session_state.status_msg)

# ---- render grid ----
table = st.session_state.latest_table

if table is None:
    st.info("Waiting for first successful fetch.")
else:
    header_cols = st.columns([1] + [2] * len(STATUSES))
    header_cols[0].markdown("**Bucket**")
    for c, status in zip(header_cols[1:], STATUSES):
        c.markdown(f"**{status}**")

    for bucket in DAY_BUCKETS:
        row_cols = st.columns([1] + [2] * len(STATUSES))
        row_cols[0].markdown(f"**{bucket}**")

        for c, status in zip(row_cols[1:], STATUSES):
            oifs = table[status][bucket]
            with c:
                st.markdown(f"({len(oifs)})")
                with st.expander("View OIFs", expanded=False):
                    if oifs:
                        st.text("\n".join(oifs))
                    else:
                        st.text("(none)")

if not HAVE_AUTOREFRESH:
    st.caption(
        "Tip: install `streamlit-autorefresh` (add it to requirements.txt) "
        "for hands-off polling; otherwise use the 'Refresh now' button."
    )
