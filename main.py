"""
Entry point for the wasted-wind curtailment cost calculator.

Replicates Module 1 of wastedwind.energy:
    Daily cost paid to curtail (switch off) wind turbines in the GB electricity market.

Usage:
    python main.py
    # Future: python main.py 2026-11-17
"""

import sys
import logging

from wasted_wind_calc import (
    get_curtailed_wind_bmus,
    get_accepted_bid_prices,
    calculate_daily_wind_bid_cost,
)

# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------
# Active: hardcoded target date (17th November – expected output ~£620k)
TARGET_DATE = "2026-11-17"

# Commented-out: user-supplied date via CLI argument
# if len(sys.argv) > 1:
#     TARGET_DATE = sys.argv[1]
# else:
#     TARGET_DATE = input("Enter target date (YYYY-MM-DD): ").strip()

# Commented-out: user-supplied date via input() only
# TARGET_DATE = input("Enter target date (YYYY-MM-DD): ").strip()


def main() -> None:
    """Orchestrate the three-step calculation and print a summary table."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print(f"{'=' * 70}")
    print(f"  Wasted Wind Curtailment Cost Calculator  |  {TARGET_DATE}")
    print(f"{'=' * 70}\n")

    # Step 1 – Identify curtailed wind BMUs
    print("[1/3] Fetching curtailed wind BMUs ...")
    acceptances = get_curtailed_wind_bmus(TARGET_DATE)
    if acceptances.empty:
        print("  No curtailed wind BMUs found for this date.")
        return
    print(f"  Found {acceptances['elexonBmUnit'].nunique()} wind BMU(s) with accepted bids.\n")

    # Step 2 – Retrieve accepted bid prices
    print("[2/3] Fetching accepted bid prices ...")
    bmu_ids = acceptances["elexonBmUnit"].unique().tolist()
    prices = get_accepted_bid_prices(TARGET_DATE, bmu_ids)
    if prices.empty:
        print("  No bid price data found for the identified BMUs.")
        return
    print(f"  Retrieved {len(prices)} price record(s).\n")

    # Step 3 – Calculate daily cost
    print("[3/3] Calculating daily wind bid cost ...")
    results = calculate_daily_wind_bid_cost(acceptances, prices, target_date=TARGET_DATE)

    if results.empty:
        print("  No cost data to display.")
        return

    # ------------------------------------------------------------------
    # Print per-BMU summary table
    # ------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print(f"  PER-BMU SUMMARY – Wind Curtailment Bids  |  {TARGET_DATE}")
    print(f"{'=' * 70}")
    print(f"  {'BMU ID':<22} {'Name':<30} {'MWh':>8} {'£/MWh':>8} {'Cost £':>12} {'SO':>4}")
    print(f"  {'-' * 22} {'-' * 30} {'-' * 8} {'-' * 8} {'-' * 12} {'-' * 4}")

    for _, row in results.iterrows():
        name = str(row["bmUnitName"])
        if len(name) > 28:
            name = name[:26] + "…"
        so = "Y" if row.get("is_system_flag", False) else ""
        print(
            f"  {row['elexonBmUnit']:<22} {name:<30} "
            f"{row['curtailed_mwh']:>8.1f} {row['avg_price']:>8.2f} "
            f"{row['total_cost']:>12.2f} {so:>4}"
        )

    # ------------------------------------------------------------------
    # Print both summary blocks
    # ------------------------------------------------------------------
    total_mwh_all = results["curtailed_mwh"].sum()
    total_cost_all = results["total_cost"].sum()
    avg_price_all = total_cost_all / total_mwh_all if total_mwh_all > 0 else 0.0

    sys_mask = results["is_system_flag"] == True  # noqa: E712
    total_mwh_sys = results.loc[sys_mask, "curtailed_mwh"].sum()
    total_cost_sys = results.loc[sys_mask, "total_cost"].sum()
    avg_price_sys = total_cost_sys / total_mwh_sys if total_mwh_sys > 0 else 0.0

    total_mwh_non = total_mwh_all - total_mwh_sys
    total_cost_non = total_cost_all - total_cost_sys
    avg_price_non = total_cost_non / total_mwh_non if total_mwh_non > 0 else 0.0

    print(f"\n{'=' * 70}")
    print(f"  DAILY TOTALS  |  {TARGET_DATE}")
    print(f"{'=' * 70}")
    print(f"")
    print(f"  ALL ACCEPTED WIND BIDS")
    print(f"  {'-' * 40}")
    print(f"    Total curtailed wind energy : {total_mwh_all:>10.1f} MWh")
    print(f"    Volume-weighted avg price   : {avg_price_all:>10.2f} £/MWh")
    print(f"    Total wind bid cost         : {total_cost_all:>12,.2f} £")
    print(f"")
    print(f"  SYSTEM / CONSTRAINT-RELATED (soFlag = True)")
    print(f"  {'-' * 40}")
    print(f"    Total curtailed wind energy : {total_mwh_sys:>10.1f} MWh")
    print(f"    Volume-weighted avg price   : {avg_price_sys:>10.2f} £/MWh")
    print(f"    Total wind bid cost         : {total_cost_sys:>12,.2f} £")
    print(f"")
    print(f"  NON-SYSTEM / ENERGY BALANCING (soFlag = False)")
    print(f"  {'-' * 40}")
    print(f"    Total curtailed wind energy : {total_mwh_non:>10.1f} MWh")
    print(f"    Volume-weighted avg price   : {avg_price_non:>10.2f} £/MWh")
    print(f"    Total wind bid cost         : {total_cost_non:>12,.2f} £")
    print(f"")
    print(f"{'=' * 70}")
    print(f"  NOTE: 'System' bids (soFlag=True) are constraint-related actions")
    print(f"  by NESO. 'Non-system' bids are energy-balancing actions.")
    print(f"  Compare 'System' total against wastedwind.energy output.")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
