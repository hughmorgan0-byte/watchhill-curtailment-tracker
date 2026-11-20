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
    /* Remove the hamburger menu / main menu */
    #MainMenu {visibility: hidden;}
    /* Remove the footer */
    footer {visibility: hidden;}
    /* Remove the header background for a cleaner embed */
    header[data-testid="stHeader"] {background: transparent; border-bottom: none;}
    /* Reduce top padding so content sits higher in the iFrame */
    .block-container {padding-top: 1.5rem; padding-bottom: 0.5rem;}
    /* Slightly tighter spacing between elements */
    .stMetric {margin-bottom: 0.25rem;}
    /* Make the date input and button sit on one row */
    div[data-testid="stHorizontalBlock"] {gap: 0.75rem;}
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
# Main dashboard
# ---------------------------------------------------------------------------
st.title("🌬️ GB Wind Curtailment & Constraint Cost")
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
# HEADLINE KPI COMPARISON CARDS
# ===================================================================
st.subheader("Headline Comparison")

left_col, right_col = st.columns(2, gap="large")

# --- Left: Unadjusted Baseline ---
with left_col:
    st.markdown("**Unadjusted Baseline** *(wastedwind.energy method)*")
    st.markdown("---")
    m1, m2 = st.columns(2)
    with m1:
        st.metric("Wind Bid Cost", f"£{total_cost_all:,.0f}")
    with m2:
        st.metric("Curtailed Wind", f"{total_mwh_all:,.1f} MWh")
    m3, m4 = st.columns(2)
    with m3:
        st.metric("Replacement Offer Cost", f"£{module2['replacement_cost_gbp']:,.0f}")
    with m4:
        unadjusted_total = total_cost_all + module2["replacement_cost_gbp"]
        st.metric("Combined Total", f"£{unadjusted_total:,.0f}")

# --- Right: NIV-Adjusted ---
with right_col:
    st.markdown("**NIV-Adjusted Constraint Cost** *(Half-Hourly, Top-of-Stack)*")
    st.markdown("---")
    if niv_result is not None:
        n1, n2 = st.columns(2)
        with n1:
            st.metric("Wind Bid Cost", f"£{total_cost_all:,.0f}")
        with n2:
            st.metric("MWh Absorbed by Long Market", f"{niv_result['total_long_market_offset_mwh']:,.1f} MWh")
        n3, n4 = st.columns(2)
        with n3:
            st.metric("NIV-Adj. Replacement Cost", f"£{niv_result['niv_adjusted_offer_cost_gbp']:,.0f}")
        with n4:
            niv_total = total_cost_all + niv_result["niv_adjusted_offer_cost_gbp"]
            st.metric("Combined Constraint Total", f"£{niv_total:,.0f}")
    else:
        st.info("NIV data unavailable for this date.")

# ===================================================================
# HALF-HOURLY CHART
# ===================================================================
st.subheader("Half-Hourly Breakdown (Settlement Periods 1–48)")

chart_df = _build_period_chart_data(results)

fig = go.Figure()

# Bar: Curtailed Wind
fig.add_trace(go.Bar(
    x=chart_df["settlementPeriod"],
    y=chart_df["wind_curtailed_mwh"],
    name="Curtailed Wind (MWh)",
    marker_color="#2196F3",
    opacity=0.85,
))

# Bar: Required Offer (NIV-adjusted)
fig.add_trace(go.Bar(
    x=chart_df["settlementPeriod"],
    y=chart_df["required_offer_mwh"],
    name="Constraint-Required Offer (MWh)",
    marker_color="#FF9800",
    opacity=0.75,
))

# Line: NIV
fig.add_trace(go.Scatter(
    x=chart_df["settlementPeriod"],
    y=chart_df["niv_mwh"],
    name="Net Imbalance Volume (MWh)",
    mode="lines+markers",
    line=dict(color="#F44336", width=2),
    marker=dict(size=4),
    yaxis="y2",
))

fig.update_layout(
    title=None,
    xaxis_title="Settlement Period",
    yaxis=dict(title="MWh (Wind / Offer)"),
    yaxis2=dict(
        title="NIV (MWh)",
        overlaying="y",
        side="right",
        showgrid=False,
    ),
    barmode="group",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    height=420,
    margin=dict(l=40, r=40, t=30, b=40),
)

st.plotly_chart(fig, use_container_width=True)

# ===================================================================
# TABS – DETAILED BREAKDOWNS
# ===================================================================
st.subheader("Detailed Breakdowns")

tab_wind, tab_replacement = st.tabs([
    "🌬️ Wind Farms Curtailed (Module 1)",
    "⚡ Replacement Generation Mix (Module 2 & 2B)",
])

# --- Tab 1: Wind Farms Curtailed ---
with tab_wind:
    display_df = bid_results[["elexonBmUnit", "bmUnitName", "curtailed_mwh",
                              "avg_price", "total_cost", "is_system_flag"]].copy()
    display_df.columns = ["BMU ID", "Name", "Curtailed MWh", "Avg £/MWh", "Cost £", "SO Flag"]
    display_df["SO Flag"] = display_df["SO Flag"].map({True: "Yes", False: "No"})
    display_df = display_df.sort_values("Cost £", ascending=False).reset_index(drop=True)

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Curtailed MWh": st.column_config.NumberColumn(format="%.1f"),
            "Avg £/MWh": st.column_config.NumberColumn(format="%.2f"),
            "Cost £": st.column_config.NumberColumn(format="£%.2f"),
        },
    )

    # Totals row
    st.markdown(
        f"**Totals:** {display_df['Curtailed MWh'].sum():.1f} MWh | "
        f"£{display_df['Cost £'].sum():,.2f}"
    )

# --- Tab 2: Replacement Generation Mix ---
with tab_replacement:
    st.markdown("#### Module 2 – Merit-Order Fuel Breakdown")
    fuel_2 = module2.get("fuel_breakdown", pd.DataFrame())
    if fuel_2 is not None and not fuel_2.empty:
        st.dataframe(
            fuel_2,
            use_container_width=True,
            hide_index=True,
            column_config={
                "mwh": st.column_config.NumberColumn("MWh", format="%.1f"),
                "cost_gbp": st.column_config.NumberColumn("Cost £", format="£%.2f"),
                "avg_price": st.column_config.NumberColumn("Avg £/MWh", format="%.2f"),
            },
        )
    else:
        st.info("No replacement offer data available.")

    st.markdown("#### Module 2B – NIV-Adjusted Fuel Breakdown")
    if niv_result is not None:
        fuel_2b = niv_result.get("fuel_breakdown", pd.DataFrame())
        if fuel_2b is not None and not fuel_2b.empty:
            st.dataframe(
                fuel_2b,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "mwh": st.column_config.NumberColumn("MWh", format="%.1f"),
                    "cost_gbp": st.column_config.NumberColumn("Cost £", format="£%.2f"),
                    "avg_price": st.column_config.NumberColumn("Avg £/MWh", format="%.2f"),
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
                "cost_gbp": st.column_config.NumberColumn("Cost £", format="£%.2f"),
            },
        )
    else:
        st.info("No top BMU data available.")

# ---------------------------------------------------------------------------
# Footer note
# ---------------------------------------------------------------------------
st.divider()
st.caption(
    f"Data source: Elexon BMRS API | Settlement date: **{results['target_date']}** | "
    f"Pipeline cached for 1 hour."
)
