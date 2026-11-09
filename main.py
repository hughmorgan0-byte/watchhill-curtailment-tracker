import logging
from datetime import datetime
import wind_bids
import constraints
import pandas as pd

def calculate_wind_cost(bid_volume: float, target_date: str) -> float:
    """
    Calculates the total cost of wind bids for a given volume and date.
    """
    # Get merit order offers
    offers = constraints.get_merit_order_offers(target_date)
    
    # Sort offers by price
    offers = offers.sort_values('offerPrice')
    
    # Calculate cumulative volume
    offers['cumulativeVolume'] = offers['offerVolume'].cumsum()
    
    # Find where cumulative volume exceeds our bid volume
    mask = offers['cumulativeVolume'] <= bid_volume
    relevant_offers = offers[mask]
    
    # Calculate total cost
    total_cost = (relevant_offers['offerVolume'] * relevant_offers['offerPrice']).sum()
    
    return total_cost / 1000000  # Convert to millions

def main(target_date="2026-10-14"):
    """
    Main function to run wind bid analysis
    """
    logging.basicConfig(level=logging.INFO)
    
    print("\nWind Bid Analysis")
    print("-" * 60)
    print(f"Date: {target_date}")
    
    try:
        # Get wind bid data
        bid_data, total_volume = wind_bids.get_wind_bid_data(target_date)
        
        # Calculate cost
        total_cost = calculate_wind_cost(total_volume, target_date)
        
        print("\nResults:")
        print(f"Total bid volume: £{total_volume:.2f} MWh")
        print(f"Total cost: £{total_cost:.2f} million")
        
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
