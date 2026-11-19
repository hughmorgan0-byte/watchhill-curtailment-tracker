"""
Entry point for the wasted-wind curtailment cost calculator.

Replicates wastedwind.energy Modules 1 & 2:
    Module 1: Daily cost paid to curtail (switch off) wind turbines.
    Module 2: Cost of buying replacement energy (accepted offers, merit order).

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
    get_accepted_offer_volumes,
    get_accepted_offer_prices,
    calculate_replacement_offer_cost,
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
    """Orchestrate Module 1 and Module 2 calculations and print summaries."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print(f"{'=' * 70}")
    print(f"  Wasted Wind Curtailment Cost Calculator  |  {TARGET_DATE}")
    print(f"{'=' * 70}\n")

    # ==================================================================
    # MODULE 1 – Wind Curtailment Bid Cost
    # ==================================================================
    print("[MODULE 1] Wind Curtailment Bid Cost")
    print(f"{'-' * 70}")

    # Step 1 – Identify curtailed wind BMUs
    print("  [1/3] Fetching curtailed wind BMUs ...")
    acceptances = get_curtailed_wind_bmus(TARGET_DATE)
    if acceptances.empty:
        print("  No curtailed wind BMUs found for this date.")
        return
    print(f"  Found {acceptances['elexonBmUnit'].nunique()} wind BMU(s) with accepted bids.\n")

    # Step 2 – Retrieve accepted bid prices
    print("  [2/3] Fetching accepted bid prices ...")
    bmu_ids = acceptances["elexonBmUnit"].unique().tolist()
    prices = get_accepted_bid_prices(TARGET_DATE, bmu_ids)
    if prices.empty:
        print("  No bid price data found for the identified BMUs.")
        return
    print(f"  Retrieved {len(prices)} price record(s).\n")

    # Step 3 – Calculate daily cost
    print("  [3/3] Calculating daily wind bid cost ...")
    bid_results = calculate_daily_wind_bid_cost(acceptances, prices, target_date=TARGET_DATE)

    if bid_results.empty:
        print("  No cost data to display.")
        return

    # Print per-BMU summary table
    print(f"\n  PER-BMU SUMMARY – Wind Curtailment Bids")
    print(f"  {'BMU ID':<22} {'Name':<30} {'MWh':>8} {'£/MWh':>8} {'Cost £':>12} {'SO':>4}")
    print(f"  {'-' * 22} {'-' * 30} {'-' * 8} {'-' * 8} {'-' * 12} {'-' * 4}")

    for _, row in bid_results.iterrows():
        name = str(row["bmUnitName"])
        if len(name) > 28:
            name = name[:26] + "…"
        so = "Y" if row.get("is_system_flag", False) else ""
        print(
            f"  {row['elexonBmUnit']:<22} {name:<30} "
            f"{row['curtailed_mwh']:>8.1f} {row['avg_price']:>8.2f} "
            f"{row['total_cost']:>12.2f} {so:>4}"
        )

    # Module 1 totals
    total_mwh_all = bid_results["curtailed_mwh"].sum()
    total_cost_all = bid_results["total_cost"].sum()
    avg_price_all = total_cost_all / total_mwh_all if total_mwh_all > 0 else 0.0

    sys_mask = bid_results["is_system_flag"] == True  # noqa: E712
    total_mwh_sys = bid_results.loc[sys_mask, "curtailed_mwh"].sum()
    total_cost_sys = bid_results.loc[sys_mask, "total_cost"].sum()
    avg_price_sys = total_cost_sys / total_mwh_sys if total_mwh_sys > 0 else 0.0

    total_mwh_non = total_mwh_all - total_mwh_sys
    total_cost_non = total_cost_all - total_cost_sys
    avg_price_non = total_cost_non / total_mwh_non if total_mwh_non > 0 else 0.0

    print(f"\n  MODULE 1 TOTALS")
    print(f"  {'-' * 40}")
    print(f"    ALL ACCEPTED WIND BIDS")
    print(f"      Total curtailed wind energy : {total_mwh_all:>10.1f} MWh")
    print(f"      Volume-weighted avg price   : {avg_price_all:>10.2f} £/MWh")
    print(f"      Total wind bid cost         : {total_cost_all:>12,.2f} £")
    print(f"")
    print(f"    SYSTEM / CONSTRAINT-RELATED (soFlag = True)")
    print(f"      Total curtailed wind energy : {total_mwh_sys:>10.1f} MWh")
    print(f"      Volume-weighted avg price   : {avg_price_sys:>10.2f} £/MWh")
    print(f"      Total wind bid cost         : {total_cost_sys:>12,.2f} £")
    print(f"")
    print(f"    NON-SYSTEM / ENERGY BALANCING (soFlag = False)")
    print(f"      Total curtailed wind energy : {total_mwh_non:>10.1f} MWh")
    print(f"      Volume-weighted avg price   : {avg_price_non:>10.2f} £/MWh")
    print(f"      Total wind bid cost         : {total_cost_non:>12,.2f} £")

    # ==================================================================
    # MODULE 2 – Replacement Energy (Accepted Offer) Merit-Order Cost
    # ==================================================================
    print(f"\n{'=' * 70}")
    print(f"  MODULE 2 – Replacement Energy Cost (Accepted Offer Merit Order)")
    print(f"{'=' * 70}")

    # Step 4 – Retrieve accepted offer volumes (all BMUs)
    print("  [1/3] Fetching accepted offer volumes (all BMUs) ...")
    offer_volumes = get_accepted_offer_volumes(TARGET_DATE)
    if offer_volumes.empty:
        print("  No accepted offer volumes found for this date.")
        return
    print(f"  Found {offer_volumes['bmUnit'].nunique()} BMU(s) with accepted offers "
          f"({offer_volumes['offer_mwh'].sum():.1f} MWh total).\n")

    # Step 5 – Retrieve accepted offer prices
    print("  [2/3] Fetching accepted offer prices ...")
    offer_bmu_ids = offer_volumes["bmUnit"].unique().tolist()
    offer_prices = get_accepted_offer_prices(TARGET_DATE, offer_bmu_ids)
    if offer_prices.empty:
        print("  No offer price data found.")
        return
    print(f"  Retrieved {len(offer_prices)} offer price record(s).\n")

    # Merge volumes and prices to form the offer stack
    offer_stack = offer_volumes.merge(
        offer_prices,
        on=["bmUnit", "settlementPeriod", "pairId"],
        how="inner",
        suffixes=("", "_price"),
    )
    # Rename the price column from the merge
    if "price" in offer_stack.columns:
        offer_stack = offer_stack.rename(columns={"price": "price"})
    elif "price_price" in offer_stack.columns:
        offer_stack = offer_stack.rename(columns={"price_price": "price"})

    if offer_stack.empty:
        print("  No matching offer volume/price records.")
        return

    print(f"  Offer stack: {len(offer_stack)} slices, "
          f"{offer_stack['offer_mwh'].sum():.1f} MWh total.\n")

    # Step 6 – Calculate merit-order replacement cost
    print("  [3/3] Calculating merit-order replacement cost ...")
    module2 = calculate_replacement_offer_cost(bid_results, offer_stack)

    # Print Module 2 results
    print(f"\n  MODULE 2 RESULTS")
    print(f"  {'-' * 40}")
    print(f"    Target curtailed wind (from Module 1) : {module2['target_mwh']:>10.1f} MWh")
    print(f"    Total accepted offers available       : {module2['total_offers_available']:>10.1f} MWh")
    print(f"    Matched replacement energy            : {module2['matched_mwh']:>10.1f} MWh")
    if module2["shortfall"]:
        print(f"    *** SHORTFALL: offers < curtailed wind ***")
    print(f"    Total replacement offer cost          : {module2['replacement_cost_gbp']:>12,.2f} £")
    print(f"    Volume-weighted avg offer price       : {module2['weighted_avg_price']:>10.2f} £/MWh")

    # Fuel type breakdown
    fuel_bd = module2["fuel_breakdown"]
    if not fuel_bd.empty:
        print(f"\n  FUEL TYPE BREAKDOWN (Merit Order)")
        print(f"  {'Fuel':<12} {'MWh':>10} {'Cost £':>14} {'£/MWh':>8}")
        print(f"  {'-' * 12} {'-' * 10} {'-' * 14} {'-' * 8}")
        for _, row in fuel_bd.iterrows():
            print(
                f"  {row['fuelType']:<12} {row['mwh']:>10.1f} "
                f"{row['cost_gbp']:>14,.2f} {row['avg_price']:>8.2f}"
            )

    # Top BMUs
    top = module2["top_bmus"]
    if not top.empty:
        print(f"\n  TOP 10 CONTRIBUTING OFFER BMUs")
        print(f"  {'BMU ID':<22} {'Name':<28} {'Fuel':<10} {'MWh':>8} {'Cost £':>12}")
        print(f"  {'-' * 22} {'-' * 28} {'-' * 10} {'-' * 8} {'-' * 12}")
        for _, row in top.iterrows():
            name = str(row["assetName"])
            if len(name) > 26:
                name = name[:24] + "…"
            print(
                f"  {row['bmUnit']:<22} {name:<28} {row['fuelType']:<10} "
                f"{row['mwh']:>8.1f} {row['cost_gbp']:>12,.2f}"
            )

    # ==================================================================
    # COMBINED HEADLINE TOTAL
    # ==================================================================
    combined_total = total_cost_all + module2["replacement_cost_gbp"]

    print(f"\n{'=' * 70}")
    print(f"  COMBINED HEADLINE TOTAL  |  {TARGET_DATE}")
    print(f"{'=' * 70}")
    print(f"")
    print(f"    Module 1 – Wind Bid Cost (curtailment)     : {total_cost_all:>14,.2f} £")
    print(f"    Module 2 – Replacement Offer Cost (merit)  : {module2['replacement_cost_gbp']:>14,.2f} £")
    print(f"    {'-' * 50}")
    print(f"    TOTAL CONSTRAINT COST (Module 1 + 2)       : {combined_total:>14,.2f} £")
    print(f"")
    print(f"  Compare against wastedwind.energy output for {TARGET_DATE}.")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
