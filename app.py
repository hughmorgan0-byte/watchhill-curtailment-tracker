"""
app.py – Streamlit dashboard for the GB Wind Curtailment & Constraint Cost calculator.

Designed to be embedded inside a Wix website iFrame.
Wraps the existing pipeline in ``wasted_wind_calc.py``.

Usage:
    streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from datetime import date, timedelta

from wasted_wind_calc import (
    get_curtailed_wind_bmus,
    get_accepted_bid_prices,
    calculate_daily_wind_bid_cost,
    get_accepted_offer_volumes,
    get_accepted_offer_prices,
    calculate_replacement_offer_cost,
    get_net_imbalance_volumes,
    calculate_niv_adjusted_offer_cost,
)

# ---------------------------------------------------------------------------
# Page configuration & embedded-widget styling
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="GB Wind Curtailment & Constraint Cost",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide Streamlit chrome so the widget looks native inside a Wix iFrame
st.markdown(
    """
    <style>
    /* ── Reset Streamlit chrome ─────────────────────────────────────── */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent; border-bottom: none;}
    .block-container {
        padding-top: 2rem;
        padding-bottom: 1rem;
        max-width: 1200px;
    }

    /* ── Typography ─────────────────────────────────────────────────── */
    h1, h2, h3, h4, h5, h6 {
        color: #F2EBD9;
        font-family: 'Inter', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
        font-weight: 600;
        letter-spacing: -0.01em;
    }
    p, span, label {
        font-family: 'Inter', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
        color: #F2EBD9;
    }
    .stCaption {
        color: #8FA69A;
        font-size: 0.85rem;
    }

    /* ── KPI Callout Cards (Red vs Blue – kept as brand highlights) ── */
    .kpi-card {
        border-radius: 10px;
        padding: 1.25rem 1.5rem;
        margin-bottom: 0.75rem;
        border: 1px solid;
    }
    .kpi-card-red {
        background: #3D2222;
        border-color: #6B3A3A;
    }
    .kpi-card-blue {
        background: #1E2D3D;
        border-color: #3A5A7B;
    }
    .kpi-card h4 {
        margin: 0 0 0.25rem 0;
        font-size: 0.8rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        font-weight: 700;
    }
    .kpi-card-red h4 { color: #F5C6C6; }
    .kpi-card-blue h4 { color: #C6D9F5; }
    .kpi-card .kpi-value {
        font-size: 1.6rem;
        font-weight: 700;
        line-height: 1.2;
    }
    .kpi-card-red .kpi-value { color: #FF6B6B; }
    .kpi-card-blue .kpi-value { color: #6BA3FF; }
    .kpi-card .kpi-sub {
        font-size: 0.78rem;
        color: #8FA69A;
        margin-top: 0.15rem;
    }

    /* ── Section panels (Dark Forest) ───────────────────────────────── */
    .section-panel {
        background: #26362D;
        border: 1px solid #3B5246;
        border-radius: 10px;
        padding: 1.25rem 1.5rem;
        margin-bottom: 1rem;
    }

    /* ── Tabs styling (Dark with Sage accent) ───────────────────────── */
    [data-testid="stTabs"] [role="tab"] {
        font-family: 'Inter', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
        font-weight: 500;
        color: #F2EBD9;
        background-color: #26362D;
        border: 1px solid #3B5246;
        border-radius: 6px 6px 0 0;
    }
    [data-testid="stTabs"] [aria-selected="true"] {
        color: #DCE6AA;
        background-color: #26362D;
        border-bottom: 2px solid #DCE6AA;
    }

    /* ── Dataframe styling (Dark) ───────────────────────────────────── */
    [data-testid="stDataFrame"] {
        border: 1px solid #3B5246;
        border-radius: 8px;
    }

    /* ── Buttons (Light Sage accent on dark) ────────────────────────── */
    .stButton > button {
        font-family: 'Inter', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
        font-weight: 600;
        border-radius: 6px;
    }
    .stButton > button[kind="primary"] {
        background-color: #DCE6AA;
        color: #1C2922;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #C8D494;
    }

    /* ── Divider (Forest Green border) ──────────────────────────────── */
    hr {
        border-color: #3B5246;
    }

    /* ── Metric cards (Dark Forest) ─────────────────────────────────── */
    [data-testid="stMetric"] {
        background-color: #26362D;
        border: 1px solid #3B5246;
        border-radius: 8px;
        padding: 0.75rem 1rem;
    }
    [data-testid="stMetric"] label {
        color: #8FA69A;
    }
    [data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #F2EBD9;
    }

    /* ── Info / Warning / Error boxes ───────────────────────────────── */
    [data-testid="stAlert"] {
        border-radius: 8px;
    }
    [data-testid="stAlert"] p {
        color: #F2EBD9;
    }

    /* ── Date input ─────────────────────────────────────────────────── */
    [data-testid="stDateInput"] input {
        background-color: #26362D;
        border: 1px solid #3B5246;
        color: #F2EBD9;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Cached pipeline
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner="Fetching Elexon BMRS data & running calculations…")
def run_pipeline(target_date: str) -> dict:
    """
    Execute the full Module 1 → Module 2 → Module 2B pipeline for a given date.

    Returns a dict containing every artefact the dashboard needs.
    """
    # --- Module 1: Wind Curtailment Bid Cost ---
    acceptances = get_curtailed_wind_bmus(target_date)
    bid_results = pd.DataFrame()
    total_cost_all = 0.0
    total_mwh_all = 0.0

    if not acceptances.empty:
        bmu_ids = acceptances["elexonBmUnit"].unique().tolist()
        prices = get_accepted_bid_prices(target_date, bmu_ids)
        if not prices.empty:
            bid_results = calculate_daily_wind_bid_cost(acceptances, prices, target_date=target_date)
            if not bid_results.empty:
                total_mwh_all = float(bid_results["curtailed_mwh"].sum())
                total_cost_all = float(bid_results["total_cost"].sum())

    # --- Module 2: Replacement Offer Merit-Order Cost ---
    module2 = {
        "target_mwh": total_mwh_all,
        "matched_mwh": 0.0,
        "replacement_cost_gbp": 0.0,
        "weighted_avg_price": 0.0,
        "fuel_breakdown": pd.DataFrame(columns=["fuelType", "mwh", "cost_gbp", "avg_price"]),
        "top_bmus": pd.DataFrame(columns=["bmUnit", "assetName", "fuelType", "mwh", "cost_gbp"]),
        "total_offers_available": 0.0,
        "shortfall": True,
    }

    offer_stack = pd.DataFrame()
    if not bid_results.empty:
        offer_volumes = get_accepted_offer_volumes(target_date)
        if not offer_volumes.empty:
            offer_bmu_ids = offer_volumes["bmUnit"].unique().tolist()
            offer_prices = get_accepted_offer_prices(target_date, offer_bmu_ids)
            if not offer_prices.empty:
                offer_stack = offer_volumes.merge(
                    offer_prices,
                    on=["bmUnit", "settlementPeriod", "pairId"],
                    how="inner",
                    suffixes=("", "_price"),
                )
                if "price" not in offer_stack.columns and "price_price" in offer_stack.columns:
                    offer_stack = offer_stack.rename(columns={"price_price": "price"})

                if not offer_stack.empty:
                    module2 = calculate_replacement_offer_cost(bid_results, offer_stack)

    # --- Module 2B: NIV-Adjusted Replacement Offer Cost ---
    niv_by_period: dict[int, float] = {}
    niv_result: dict | None = None

    if not bid_results.empty and not offer_stack.empty:
        niv_by_period = get_net_imbalance_volumes(target_date)
        if niv_by_period:
            niv_result = calculate_niv_adjusted_offer_cost(
                bid_records=acceptances,
                offer_stack=offer_stack,
                niv_by_period=niv_by_period,
            )

    return {
        "target_date": target_date,
        "acceptances": acceptances,
        "bid_results": bid_results,
        "module2": module2,
        "offer_stack": offer_stack,
        "niv_by_period": niv_by_period,
        "niv_result": niv_result,
        "total_mwh_all": total_mwh_all,
        "total_cost_all": total_cost_all,
    }


# ---------------------------------------------------------------------------
# Helper: build per-period chart data
# ---------------------------------------------------------------------------
def _build_period_chart_data(results: dict) -> pd.DataFrame:
    """
    Construct a DataFrame with one row per settlement period (1-48) containing:
      - wind_curtailed_mwh
      - niv_mwh
      - required_offer_mwh
    """
    rows = []
    acceptances = results["acceptances"]
    niv_by_period = results["niv_by_period"]
    niv_result = results["niv_result"]

    # Wind curtailed per period
    wind_by_period: dict[int, float] = {}
    if not acceptances.empty and "settlementPeriod" in acceptances.columns:
        tmp = acceptances.copy()
        tmp["abs_mwh"] = tmp["acceptedVolumeMWh"].abs()
        wind_by_period = tmp.groupby("settlementPeriod")["abs_mwh"].sum().to_dict()

    # Required offer per period (from NIV-adjusted period_detail)
    required_by_period: dict[int, float] = {}
    if niv_result is not None:
        pd_detail = niv_result.get("period_detail")
        if pd_detail is not None and not pd_detail.empty:
            required_by_period = dict(
                zip(pd_detail["settlementPeriod"].astype(int), pd_detail["required_mwh"])
            )

    for sp in range(1, 49):
        rows.append({
            "settlementPeriod": sp,
            "wind_curtailed_mwh": wind_by_period.get(sp, 0.0),
            "niv_mwh": niv_by_period.get(sp, 0.0),
            "required_offer_mwh": required_by_period.get(sp, 0.0),
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Helper: render a KPI callout card via raw HTML
# ---------------------------------------------------------------------------
def _kpi_card(label: str, value: str, sub: str = "", variant: str = "blue") -> str:
    """Return an HTML string for a styled KPI card."""
    cls = f"kpi-card kpi-card-{variant}"
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="{cls}">'
        f'<h4>{label}</h4>'
        f'<div class="kpi-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------
st.title("GB Wind Curtailment & Constraint Cost")
st.caption("Replicates wastedwind.energy Modules 1, 2 & 2B using Elexon BMRS data.")

# --- Date selector ---
default_date = date.today() - timedelta(days=1)
col_date, col_btn = st.columns([2, 1], vertical_alignment="bottom")
with col_date:
    selected_date = st.date_input(
        label="Settlement Date",
        value=default_date,
        format="DD/MM/YYYY",
        key="target_date_input",
    )
with col_btn:
    run_clicked = st.button("Run Analysis", type="primary", use_container_width=True)

# --- Execute pipeline ---
results: dict | None = None
if run_clicked:
    target_date_str = selected_date.strftime("%Y-%m-%d")
    results = run_pipeline(target_date_str)

# Also auto-run on first load for convenience
if results is None and "has_run" not in st.session_state:
    target_date_str = default_date.strftime("%Y-%m-%d")
    results = run_pipeline(target_date_str)
    st.session_state["has_run"] = True

# Reset flag when user changes date so next button click re-runs
if run_clicked:
    st.session_state["has_run"] = False

# ---------------------------------------------------------------------------
# Render results
# ---------------------------------------------------------------------------
if results is None:
    st.info("Select a date and click **Run Analysis** to begin.")
    st.stop()

bid_results: pd.DataFrame = results["bid_results"]
module2: dict = results["module2"]
niv_result: dict | None = results["niv_result"]
total_mwh_all: float = results["total_mwh_all"]
total_cost_all: float = results["total_cost_all"]

if bid_results.empty:
    st.warning(f"No curtailed wind BMUs found for **{results['target_date']}**. "
               "Try a different date.")
    st.stop()

# ===================================================================
# HEADLINE KPI COMPARISON CARDS  (Red vs Blue)
# ===================================================================
st.subheader("Headline Comparison")

left_col, right_col = st.columns(2, gap="large")

# --- Left: Unadjusted Baseline (RED) ---
with left_col:
    unadjusted_total = total_cost_all + module2["replacement_cost_gbp"]
    st.markdown(
        _kpi_card(
            "Unadjusted Baseline",
            f"\u00a3{unadjusted_total:,.0f}",
            sub=f"Wind \u00a3{total_cost_all:,.0f} + Replacement \u00a3{module2['replacement_cost_gbp']:,.0f}  |  {total_mwh_all:,.1f} MWh curtailed",
            variant="red",
        ),
        unsafe_allow_html=True,
    )
    m1, m2 = st.columns(2)
    with m1:
        st.metric("Wind Bid Cost", f"\u00a3{total_cost_all:,.0f}")
    with m2:
        st.metric("Curtailed Wind", f"{total_mwh_all:,.1f} MWh")
    m3, m4 = st.columns(2)
    with m3:
        st.metric("Replacement Offer", f"\u00a3{module2['replacement_cost_gbp']:,.0f}")
    with m4:
        st.metric("Combined Total", f"\u00a3{unadjusted_total:,.0f}")

# --- Right: NIV-Adjusted (BLUE) ---
with right_col:
    if niv_result is not None:
        niv_total = total_cost_all + niv_result["niv_adjusted_offer_cost_gbp"]
        st.markdown(
            _kpi_card(
                "NIV-Adjusted Constraint Cost",
                f"\u00a3{niv_total:,.0f}",
                sub=f"Wind \u00a3{total_cost_all:,.0f} + NIV-Adj. \u00a3{niv_result['niv_adjusted_offer_cost_gbp']:,.0f}  |  {niv_result['total_long_market_offset_mwh']:,.1f} MWh absorbed",
                variant="blue",
            ),
            unsafe_allow_html=True,
        )
        n1, n2 = st.columns(2)
        with n1:
            st.metric("Wind Bid Cost", f"\u00a3{total_cost_all:,.0f}")
        with n2:
            st.metric("MWh Absorbed (NIV<0)", f"{niv_result['total_long_market_offset_mwh']:,.1f} MWh")
        n3, n4 = st.columns(2)
        with n3:
            st.metric("NIV-Adj. Replacement", f"\u00a3{niv_result['niv_adjusted_offer_cost_gbp']:,.0f}")
        with n4:
            st.metric("Combined Total", f"\u00a3{niv_total:,.0f}")
    else:
        st.markdown(
            _kpi_card(
                "NIV-Adjusted Constraint Cost",
                "N/A",
                sub="NIV data unavailable for this date",
                variant="blue",
            ),
            unsafe_allow_html=True,
        )
        st.info("NIV data unavailable for this date.")

# ===================================================================
# HALF-HOURLY CHART
# ===================================================================
st.subheader("Half-Hourly Breakdown (Settlement Periods 1\u201348)")

chart_df = _build_period_chart_data(results)

fig = go.Figure()

# Bar: Curtailed Wind (Red highlight)
fig.add_trace(go.Bar(
    x=chart_df["settlementPeriod"],
    y=chart_df["wind_curtailed_mwh"],
    name="Curtailed Wind (MWh)",
    marker_color="#FF6B6B",
    opacity=0.85,
))

# Bar: Required Offer (Blue highlight)
fig.add_trace(go.Bar(
    x=chart_df["settlementPeriod"],
    y=chart_df["required_offer_mwh"],
    name="Constraint-Required Offer (MWh)",
    marker_color="#6BA3FF",
    opacity=0.75,
))

# Line: NIV (Light Sage – secondary chart series)
fig.add_trace(go.Scatter(
    x=chart_df["settlementPeriod"],
    y=chart_df["niv_mwh"],
    name="Net Imbalance Volume (MWh)",
    mode="lines+markers",
    line=dict(color="#DCE6AA", width=2),
    marker=dict(size=4),
    yaxis="y2",
))

fig.update_layout(
    title=None,
    xaxis_title="Settlement Period",
    xaxis=dict(gridcolor="#26362D", zerolinecolor="#3B5246"),
    yaxis=dict(title="MWh (Wind / Offer)", gridcolor="#26362D", zerolinecolor="#3B5246"),
    yaxis2=dict(
        title="NIV (MWh)",
        overlaying="y",
        side="right",
        showgrid=False,
    ),
    barmode="group",
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        xanchor="right",
        x=1,
        font=dict(color="#F2EBD9"),
    ),
    height=420,
    margin=dict(l=40, r=40, t=30, b=40),
    font=dict(family="Inter, Segoe UI, Helvetica Neue, Arial, sans-serif", size=12, color="#F2EBD9"),
    plot_bgcolor="#1C2922",
    paper_bgcolor="#1C2922",
)

st.plotly_chart(fig, use_container_width=True)

# ===================================================================
# TABS – DETAILED BREAKDOWNS
# ===================================================================
st.subheader("Detailed Breakdowns")

tab_wind, tab_replacement = st.tabs([
    "Wind Farms Curtailed (Module 1)",
    "Replacement Generation Mix (Module 2 & 2B)",
])

# --- Tab 1: Wind Farms Curtailed ---
with tab_wind:
    display_df = bid_results[["elexonBmUnit", "bmUnitName", "curtailed_mwh",
                              "avg_price", "total_cost", "is_system_flag"]].copy()
    display_df.columns = ["BMU ID", "Name", "Curtailed MWh", "Avg \u00a3/MWh", "Cost \u00a3", "SO Flag"]
    display_df["SO Flag"] = display_df["SO Flag"].map({True: "Yes", False: "No"})
    display_df = display_df.sort_values("Cost \u00a3", ascending=False).reset_index(drop=True)

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Curtailed MWh": st.column_config.NumberColumn(format="%.1f"),
            "Avg \u00a3/MWh": st.column_config.NumberColumn(format="%.2f"),
            "Cost \u00a3": st.column_config.NumberColumn(format="\u00a3%.2f"),
        },
    )

    # Extract column names to avoid backslash-in-f-string-expression issue (Python 3.11)
    _col_mwh = "Curtailed MWh"
    _col_cost = "Cost \u00a3"
    _total_mwh = display_df[_col_mwh].sum()
    _total_cost = display_df[_col_cost].sum()
    st.markdown(
        f"**Totals:** {_total_mwh:.1f} MWh | \u00a3{_total_cost:,.2f}"
    )

# --- Tab 2: Replacement Generation Mix ---
with tab_replacement:
    st.markdown("#### Module 2 \u2013 Merit-Order Fuel Breakdown")
    fuel_2 = module2.get("fuel_breakdown", pd.DataFrame())
    if fuel_2 is not None and not fuel_2.empty:
        st.dataframe(
            fuel_2,
            use_container_width=True,
            hide_index=True,
            column_config={
                "mwh": st.column_config.NumberColumn("MWh", format="%.1f"),
                "cost_gbp": st.column_config.NumberColumn("Cost \u00a3", format="\u00a3%.2f"),
                "avg_price": st.column_config.NumberColumn("Avg \u00a3/MWh", format="%.2f"),
            },
        )
    else:
        st.info("No replacement offer data available.")

    st.markdown("#### Module 2B \u2013 NIV-Adjusted Fuel Breakdown")
    if niv_result is not None:
        fuel_2b = niv_result.get("fuel_breakdown", pd.DataFrame())
        if fuel_2b is not None and not fuel_2b.empty:
            st.dataframe(
                fuel_2b,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "mwh": st.column_config.NumberColumn("MWh", format="%.1f"),
                    "cost_gbp": st.column_config.NumberColumn("Cost \u00a3", format="\u00a3%.2f"),
                    "avg_price": st.column_config.NumberColumn("Avg \u00a3/MWh", format="%.2f"),
                },
            )
        else:
            st.info("No NIV-adjusted offer data available.")
    else:
        st.info("NIV data unavailable for this date.")

    st.markdown("#### Top 10 Contributing Offer BMUs (Module 2)")
    top_bmus = module2.get("top_bmus", pd.DataFrame())
    if top_bmus is not None and not top_bmus.empty:
        st.dataframe(
            top_bmus,
            use_container_width=True,
            hide_index=True,
            column_config={
                "mwh": st.column_config.NumberColumn("MWh", format="%.1f"),
                "cost_gbp": st.column_config.NumberColumn("Cost \u00a3", format="\u00a3%.2f"),
            },
        )
    else:
        st.info("No top BMU data available.")

# ---------------------------------------------------------------------------
# Footer note
# ---------------------------------------------------------------------------
st.divider()
st.caption(
    f"Data source: Elexon BMRS API  |  Settlement date: **{results['target_date']}**  |  "
    f"Pipeline cached for 1 hour."
)
