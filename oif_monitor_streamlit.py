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
    - A grid of Status x Day-bucket cells, each showing an OIF count
      (large, centered, on a light gray background) plus the full
      OIF list underneath, always visible (no expander)

Run locally:   streamlit run oif_monitor_streamlit.py
Deploy notes:  see the bottom of this file / the accompanying README
               for PythonAnywhere-specific instructions.

Token handling: instead of a TOKEN.py file (which doesn't fit a web
deployment well), this reads the token in this order:
    1. st.secrets["TOKEN"]           (recommended - .streamlit/secrets.toml)
    2. environment variable OIF_TOKEN
    3. a manual text-input box in the sidebar (session-only, not saved)

Day-bucket freshness: the raw API data is only re-pulled on a fetch
(manual click or the configured auto-refresh interval), but the
Same Day / 1 Day / >2 Days buckets are recomputed from that raw data
on *every* Streamlit rerun using the current Adelaide date at render
time (see adelaide_today()). That way, if the day rolls over while
the page is left open between fetches, the buckets update immediately
rather than waiting for the next API pull.

Business-day bucketing: "1 Day" and ">2 Days" are counted in working
days (Mon-Fri) only - weekends are skipped when deciding how far away
an ETP is. So an ETP that falls on the next business day (e.g. Friday
-> Monday) is still "1 Day", not ">2 Days", and Saturday/Sunday are
never counted as the "1 day away" business day themselves. See
business_days_between() / get_day_bucket().

Timezone: all "today"/"now" values used by this app (bucketing and
the "Last updated" timestamp) are anchored to Australia/Adelaide via
Python's zoneinfo, regardless of what timezone the host server runs
in (e.g. PythonAnywhere typically runs UTC). If deploying and you hit
a "No time zone found" error, install the `tzdata` package (add it to
requirements.txt) - some minimal Linux images don't ship the IANA
timezone database that zoneinfo needs.
"""

import html
import os
from datetime import date, datetime

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - very old Python fallback
    from backports.zoneinfo import ZoneInfo

# All "today"/"now" references in this app are anchored to Adelaide,
# South Australia, regardless of what timezone the host server (e.g.
# PythonAnywhere) runs in. Adelaide observes daylight saving (ACST/ACDT,
# UTC+9:30 / +10:30), so a fixed offset would drift twice a year -
# ZoneInfo("Australia/Adelaide") handles that transition correctly.
ADELAIDE_TZ = ZoneInfo("Australia/Adelaide")


def adelaide_now():
    """Current datetime in Adelaide, independent of server timezone."""
    return datetime.now(ADELAIDE_TZ)


def adelaide_today():
    """Current date in Adelaide, independent of server timezone."""
    return adelaide_now().date()

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


def business_days_between(start_date, end_date):
    """Number of business days (Mon-Fri) in the half-open range [start_date, end_date).

    Weekends are skipped entirely, so e.g. a Friday `start_date` and a
    Monday `end_date` returns 1 (only the Friday counts - the
    intervening Sat/Sun are not business days). `start_date` and
    `end_date` are plain `datetime.date` objects; `end_date` is
    expected to be on or after `start_date`.
    """
    return int(np.busday_count(start_date, end_date))


def get_day_bucket(etp_str, today=None):
    """Map a 'YYYY-MM-DD' ETP string to a DAY_BUCKETS entry relative to `today`.

    `today` defaults to the current date in Adelaide (see adelaide_today()),
    not the host server's local date.

    Bucketing counts working days (Mon-Fri) only:
      - "Same Day"  : ETP is today or in the past
      - "1 Day"     : ETP is the next business day (weekends don't
                       count, so a Friday -> Monday ETP is still "1 Day")
      - ">2 Days"   : ETP is two or more business days away
    """
    if not etp_str:
        return None
    if today is None:
        today = adelaide_today()

    try:
        etp_date = datetime.strptime(etp_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

    if etp_date <= today:
        return "Same Day"

    business_days_ahead = business_days_between(today, etp_date)

    # business_days_ahead is normally >=1 once etp_date > today, but
    # can be 0 if `today` itself falls on a weekend (e.g. the app is
    # left open over a Saturday) - treat that the same as "1 Day"
    # since the very next business day is still the nearest one.
    if business_days_ahead <= 1:
        return "1 Day"
    else:
        return ">2 Days"


def build_monitor_table(df, today=None):
    """Turn a build_oif_df() DataFrame into {status: {bucket: [OIF, ...]}}.

    `today` is resolved fresh (Adelaide's current date, via
    adelaide_today()) on every call unless explicitly overridden, so
    callers that want up-to-the-render-moment buckets should simply
    call this with no `today` argument each time they render, rather
    than caching its output across reruns.
    """
    today = today or adelaide_today()
    table = {status: {bucket: [] for bucket in DAY_BUCKETS} for status in STATUSES}

    if df is None or df.empty:
        return table

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
# NOTE: we now cache the RAW dataframe, not the pre-bucketed table.
# The bucketed table is rebuilt from this raw df on every rerun using
# today's date at render time, so bucket boundaries never go stale
# between fetches even if the day rolls over while the page is open.
st.session_state.setdefault("latest_df", None)
st.session_state.setdefault("status_msg", "Not fetched yet.")
st.session_state.setdefault("auto_refresh", True)
st.session_state.setdefault("interval_seconds", DEFAULT_REFRESH_SECONDS)

title_col, refresh_col = st.columns([6, 1])
with title_col:
    st.title("OIF Monitor")
with refresh_col:
    st.write("")  # vertical spacer so the button lines up with the title
    refresh_clicked = st.button("Refresh now", use_container_width=True)

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

    if HAVE_AUTOREFRESH:
        st.caption("Auto-refresh is active via streamlit-autorefresh.")
    else:
        st.caption(
            "streamlit-autorefresh isn't installed, so auto-refresh only "
            "happens while you're interacting with the page (or add "
            "`streamlit-autorefresh` to requirements.txt for true "
            "background polling)."
        )

    st.caption(
        f"Adelaide time now: {adelaide_now().strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"(bucketing uses this date, working days only)"
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
        st.session_state.latest_df = df
        ts = adelaide_now().strftime("%Y-%m-%d %H:%M:%S %Z")
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
    or st.session_state.latest_df is None
    or (HAVE_AUTOREFRESH and st.session_state.auto_refresh)
)

if should_fetch:
    fetch_and_store()

status_placeholder.caption(st.session_state.status_msg)

# ---- rebuild the bucketed table fresh on every rerun ----
# This is the key fix: bucketing always uses "now", even on reruns
# that didn't trigger a new API fetch (e.g. someone just toggled a
# sidebar control, or the day changed since the last fetch).
table = build_monitor_table(st.session_state.latest_df)

# CSS for the grid: uniform cell size per row, a light-gray count
# section (big, bold, centered) sitting above an always-visible,
# scrollable list of every OIF number in that cell (no expander).
st.markdown(
    """
    <style>
    .oif-grid {
        display: grid;
        grid-template-columns: 140px repeat(3, 1fr);
        gap: 10px;
        margin-top: 10px;
        align-items: stretch;
    }
    .oif-header {
        font-weight: 700;
        text-align: center;
        padding: 6px 4px;
    }
    .oif-bucket-label {
        font-weight: 700;
        display: flex;
        align-items: center;
    }
    .oif-cell {
        display: flex;
        flex-direction: column;
        border: 1px solid rgba(128, 128, 128, 0.4);
        border-radius: 8px;
        overflow: hidden;
        height: 260px;
    }
    .oif-count {
        background-color: #e0e0e0;
        color: #111111;
        font-size: 20px;
        font-weight: 700;
        text-align: center;
        padding: 10px 0;
        flex-shrink: 0;
    }
    .oif-list {
        flex: 1 1 auto;
        overflow-y: auto;
        padding: 8px 10px;
        font-family: "Source Code Pro", monospace;
        font-size: 13px;
        line-height: 1.5;
    }
    .oif-list-empty {
        color: #999999;
        font-style: italic;
        text-align: center;
        margin-top: 10px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if st.session_state.latest_df is None:
    st.info("Waiting for first successful fetch.")
else:
    grid_html = ['<div class="oif-grid">']

    grid_html.append('<div class="oif-header"></div>')
    for status in STATUSES:
        grid_html.append(f'<div class="oif-header">{html.escape(status)}</div>')

    for bucket in DAY_BUCKETS:
        grid_html.append(
            f'<div class="oif-bucket-label">{html.escape(bucket)}</div>'
        )
        for status in STATUSES:
            oifs = table[status][bucket]
            count = len(oifs)

            if oifs:
                list_body = "<br>".join(html.escape(str(oif)) for oif in oifs)
                list_html = f'<div class="oif-list">{list_body}</div>'
            else:
                list_html = '<div class="oif-list"><div class="oif-list-empty">(none)</div></div>'

            grid_html.append(
                '<div class="oif-cell">'
                f'<div class="oif-count">{count}</div>'
                f"{list_html}"
                "</div>"
            )

    grid_html.append("</div>")
    st.markdown("".join(grid_html), unsafe_allow_html=True)

if not HAVE_AUTOREFRESH:
    st.caption(
        "Tip: install `streamlit-autorefresh` (add it to requirements.txt) "
        "for hands-off polling; otherwise use the 'Refresh now' button."
    )
