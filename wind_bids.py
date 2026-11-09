import pandas as pd
import requests
from datetime import datetime

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1"

def get_registered_wind_bmus() -> list:
    """
    Fetches the complete list of registered wind BMUs from the Elexon API.
    """
    endpoint = f"{BASE_URL}/unit/register"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    params = {
        "filter": {
            "FuelType": "Wind",
            "UnitType": "Generating"
        },
        "format": "json"
    }
    
    response = requests.post(endpoint, json=params, headers=headers)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    data = response.json().get('data', [])
    return [row.get('bmUnit') for row in data if row.get('bmUnit')]

def get_wind_bid_data(target_date: str) -> pd.DataFrame:
    """
    Fetches wind bid data for a given date and calculates the total bid volume.
    """
    wind_bmus = get_registered_wind_bmus()
    endpoint = f"{BASE_URL}/balancing/acceptances"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    from_timestamp = datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00")
    to_timestamp = datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y-%m-%dT23:59:59")
    
    params = {
        "settlementDate": target_date.replace("-", ""),
        "settlementPeriod": "1-48",
        "format": "json",
        "from": from_timestamp,
        "to": to_timestamp,
        "BmUnit": ",".join(wind_bmus)
    }
    
    response = requests.get(endpoint, params=params, headers=headers)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    data = response.json().get('data', [])
    df = pd.DataFrame(data)
    
    if not df.empty and 'levelFrom' in df.columns and 'levelTo' in df.columns:
        df['wind_bid_volume'] = (df['levelFrom'] + df['levelTo']) / 2  # MW
        df['wind_bid_volume'] *= 0.5  # Convert to MWh
        
        # Aggregate by settlement period
        aggregated = df.groupby('settlementPeriod')['wind_bid_volume'].sum().reset_index()
        total_volume = aggregated['wind_bid_volume'].sum()
        
        print(f"\nWind Bid Data for {target_date}")
        print("-" * 60)
        print(f"Total bid volume: £{total_volume:.2f} MWh")
        print("\nDetailed Bid Data:")
        print(aggregated)
        
        return aggregated
    
    return pd.DataFrame()
