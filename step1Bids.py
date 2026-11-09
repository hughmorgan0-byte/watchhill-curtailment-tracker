import logging
import requests
import pandas as pd
from datetime import datetime

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1"

def fetch_wind_bmus() -> list:
    """
    Fetches the complete list of registered wind BMUs from the Elexon API.
    """
    endpoint = f"{BASE_URL}/reference/bmunits/all"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    response = requests.get(endpoint, headers=headers)
    response.raise_for_status()
    
    data = response.json().get('bmUnits', [])
    wind_bmus = [unit['bmUnit'] for unit in data if unit.get('fuelType') == "WIND"]
    
    return wind_bmus

def fetch_accepted_bids(target_date: str, wind_bmus: list) -> pd.DataFrame:
    """
    Fetches accepted bids for wind BMUs on a given date.
    """
    endpoint = f"{BASE_URL}/datasets/BOALF"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    params = {
        "settlementDate": target_date.replace("-", ""),
        "bmUnit": ",".join(wind_bmus),
        "format": "json"
    }
    
    response = requests.get(endpoint, params=params, headers=headers)
    response.raise_for_status()
    
    data = response.json().get('data', [])
    df = pd.DataFrame(data)
    
    if not df.empty:
        df['wind_bid_volume'] = df['acceptedVolume']  # Assuming acceptedVolume is in MWh
        total_volume = df['wind_bid_volume'].sum()
        
        return df, total_volume
    
    return pd.DataFrame(), 0.0

def fetch_bid_offer_prices(target_date: str) -> pd.DataFrame:
    """
    Fetches bid-offer prices for a given date.
    """
    endpoint = f"{BASE_URL}/datasets/BOD"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    params = {
        "settlementDate": target_date.replace("-", ""),
        "format": "json"
    }
    
    response = requests.get(endpoint, params=params, headers=headers)
    response.raise_for_status()
    
    data = response.json().get('data', [])
    df = pd.DataFrame(data)
    
    return df

def calculate_wind_cost(bid_data: pd.DataFrame, bid_prices: pd.DataFrame) -> float:
    """
    Calculates the total cost of wind bids for a given volume and date.
    """
    merged_data = pd.merge(bid_data, bid_prices, on=['bmUnit', 'settlementPeriod'])
    merged_data['cost'] = merged_data['wind_bid_volume'] * merged_data['bidPrice']
    total_cost = merged_data['cost'].sum()
    
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
        # Fetch wind BMUs
        wind_bmus = fetch_wind_bmus()
        
        # Fetch accepted bids
        bid_data, total_volume = fetch_accepted_bids(target_date, wind_bmus)
        
        # Fetch bid-offer prices
        bid_prices = fetch_bid_offer_prices(target_date)
        
        # Calculate cost
        total_cost = calculate_wind_cost(bid_data, bid_prices)
        
        print("\nResults:")
        print(f"Total bid volume: £{total_volume:.2f} MWh")
        print(f"Total cost: £{total_cost:.2f} million")
        
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
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
    
    # Sort offers by price/test
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
    
    # Sort offers by price/test
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
