import pandas as pd
import requests
from datetime import datetime

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1"

def get_wind_bids(target_date: str, wind_bmus: list) -> pd.DataFrame:
    """
    Fetches Market-wide Bid Acceptance Volumes (BOAV) and aggregates 
    turn-down (bid) volume for Wind BMUs per settlement period.
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
    
    # Print key results
    total_volume = aggregated['wind_bid_off_volume'].sum()
    avg_bid = aggregated['wind_bid_off_volume'].mean()
    print(f"\nWind Bids for {target_date}:")
    print(f"- Total bid volume: {total_volume:.2f} MWh")
    print(f"- Average bid per period: {avg_bid:.2f} MWh")
    print(f"- Number of settlement periods with bids: {len(aggregated)}")
    
    return aggregated
