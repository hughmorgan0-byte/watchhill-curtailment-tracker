import logging
from datetime import datetime
import step1Bids

def main(target_date="2026-10-14"):
    """
    Main function to run wind bid analysis
    """
    logging.basicConfig(level=logging.INFO)
    
    print("\nWind Bid Analysis")
    print("-" * 60)
    print(f"Date: {target_date}")
    
    try:
        # Calculate wind cost
        total_cost = step1Bids.main(target_date)
        
        print("\nResults:")
        print(f"Total cost: £{total_cost:.2f} million")
        
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
