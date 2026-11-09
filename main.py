import logging
from datetime import datetime
import wind_bids
import constraints

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
        bid_data = wind_bids.get_wind_bid_data(target_date)
        
        # Run full analysis
        constraints.calculate_adjusted_constraint_cost(target_date)
        
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
