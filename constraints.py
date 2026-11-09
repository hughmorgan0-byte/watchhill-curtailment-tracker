import pandas as pd
import requests
from datetime import datetime
import itertools
import wind_bids

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1"

def get_registered_wind_bmus() -> list:
    """
    Fetches the complete list of registered wind BMUs from the Elexon API.
    """
    endpoint = f"{BASE_URL}/unit/register"
    
    # Use POST request with properly formatted parameters
    params = {
        "filter": {
            "FuelType": "Wind",
            "UnitType": "Generating"
        },
        "format": "json"
    }
    
    response = requests.post(endpoint, json=params)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    response = requests.get(endpoint, params=params)
    
    # Try with different API parameter casing if needed
    if response.status_code == 404:
        params = {
            "fuelType": "Wind",
            "format": "json"
        }
        response = requests.get(endpoint, params=params)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    data = []
    while True:
        current_data = response.json().get('data', [])
        data.extend(current_data)
        
        # Check for pagination
        next_page = response.links.get('next', {}).get('url')
        if next_page:
            response = requests.get(next_page)
        else:
            break
    
    return [row.get('bmUnit') for row in data if row.get('bmUnit')]

def get_wind_bids(target_date: str, wind_bmus: list) -> pd.DataFrame:
    """
    Fetches Market-wide Bid Acceptance Volumes (BOAV) and aggregates 
    turn-down (bid) volume for Wind BMUs per settlement period.
    
    Now includes NIV stack and cost calculations.
    """
    # Use the explicit Acceptance Volumes (BOAV) endpoint for Bids
    endpoint = f"{BASE_URL}/balancing/acceptances"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    # Format timestamps for API parameters
    # From: Start of day
    # To: End of day
    from_timestamp = datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00")
    to_timestamp = datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y-%m-%dT23:59:59")
    
    params = {
        "settlementDate": target_date.replace("-", ""),  # API requires YYYYMMDD format
        "settlementPeriod": "1-48",  # Request all settlement periods in one call
        "format": "json",
        "from": from_timestamp,
        "to": to_timestamp,
        "BmUnit": ",".join(wind_bmus)  # Filter by our list of wind BMUs
    }
    
    response = requests.get(endpoint, params=params, headers=headers)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    data = response.json().get('data', [])
    df = pd.DataFrame(data)
    
    if df.empty:
        return pd.DataFrame({'settlementPeriod': range(1, 49), 'wind_bid_off_volume': 0.0})

    # The acceptances endpoint provides 'levelFrom' and 'levelTo' in MW
    # We'll calculate the average bid volume
    if 'levelFrom' in df.columns and 'levelTo' in df.columns:
        df['wind_bid_off_volume'] = (df['levelFrom'] + df['levelTo']) / 2
    else:
        return pd.DataFrame({'settlementPeriod': range(1, 49), 'wind_bid_off_volume': 0.0})

    # Convert MW to MWh by multiplying by 0.5 (since each SP is 30 minutes)
    df['wind_bid_off_volume'] *= 0.5
    
    sp_col = next((c for c in ['settlementPeriod', 'settlementPeriodFrom'] if c in df.columns), 'settlementPeriod')
    df['settlementPeriod'] = pd.to_numeric(df[sp_col])
    
    # Aggregate total volume per settlement period
    aggregated = df.groupby('settlementPeriod')['wind_bid_off_volume'].sum().reset_index()
    
    # Calculate total volume and average bid
    total_volume = aggregated['wind_bid_off_volume'].sum()
    avg_bid = aggregated['wind_bid_off_volume'].mean()
    
    # Calculate NIV stack
    niv_stack = calculate_niv_impact(target_date)
    
    # Calculate adjusted bid-off volume
    adjusted_volume = total_volume - niv_stack
    
    # Get merit order offers
    offers = get_merit_order_offers(target_date)
    
    # Calculate cost
    total_cost = calculate_cost(offers, adjusted_volume)
    
    # Print results
    print("\nWind Bid Analysis Results")
    print("-" * 80)
    print(f"Date: {target_date}")
    print(f"Total bid-off volume: £{total_volume:.2f} MWh")
    print(f"NIV stack impact: £{niv_stack:.2f} MWh")
    print(f"Adjusted volume: £{adjusted_volume:.2f} MWh")
    print(f"Total cost: £{total_cost:.2f} million")
    
    return aggregated

def calculate_niv_impact(target_date: str) -> float:
    """
    Calculates the NIV (National Intergrestrial Variability) stack impact
    for the given date.
    """
    # API endpoint for NIV data
    endpoint = f"{BASE_URL}/niv/{target_date}"
    
    response = requests.get(endpoint)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    data = response.json().get('data', [])
    return sum(row.get('nivVolume', 0) for row in data)

def get_merit_order_offers(target_date: str) -> pd.DataFrame:
    """
    Fetches merit order offers for the given date.
    """
    endpoint = f"{BASE_URL}/meritorder/offers"
    
    params = {
        "date": target_date,
        "format": "json"
    }
    
    response = requests.get(endpoint, params=params)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    data = response.json().get('data', [])
    return pd.DataFrame(data)

def calculate_cost(offers: pd.DataFrame, volume: float) -> float:
    """
    Calculates the cost based on merit order offers up to the given volume.
    """
    # Sort offers by price
    offers = offers.sort_values('offerPrice')
    
    # Calculate cumulative volume
    offers['cumulativeVolume'] = offers['offerVolume'].cumsum()
    
    # Find where cumulative volume exceeds our adjusted volume
    mask = offers['cumulativeVolume'] <= volume
    relevant_offers = offers[mask]
    
    # Calculate total cost
    total_cost = (relevant_offers['offerVolume'] * relevant_offers['offerPrice']).sum()
    
    return total_cost / 1000000  # Convert to millions

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    
    target_date = "2026-10-14"  # Specific date for comparison
    
    print("\nRunning Wind Bids Analysis...")
    print(f"Date: {target_date}")
    
    try:
        wind_bids.get_wind_bid_data(target_date)
        
    except Exception as e:
        print(f"Error: {str(e)}")
