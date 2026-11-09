import pandas as pd
import requests
from datetime import datetime

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1"

def is_within_range(value, lower_bound, upper_bound):
    """Check if a value is within a specified range (inclusive).
    
    Args:
        value (float|int): The value to check
        lower_bound (float|int): The minimum allowed value
        upper_bound (float|int): The maximum allowed value
        
    Returns:
        bool: True if value is within range, False otherwise
    """
    return lower_bound <= value <= upper_bound

def is_type(value, expected_type):
    """Check if a value is of a specified type.
    
    Args:
        value: The value to check
        expected_type (type): The type we expect the value to be
        
    Returns:
        bool: True if value is of expected_type, False otherwise
    """
    return type(value) is expected_type

def get_wind_bmu_ids() -> list:
    """Fetches registered BM Units from Elexon and filters for Wind generators."""
    url = f"{BASE_URL}/reference/bmunits/all"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    
    units = data.get('data', data) if isinstance(data, dict) else data
    df_units = pd.DataFrame(units)
    
    fuel_col = next((c for c in ['fuelType', 'fuel_type', 'psrType'] if c in df_units.columns), None)
    id_col = next((c for c in ['bmUnit', 'nationalGridBmUnitId', 'elexonBmUnitId', 'elexonBmUnit'] if c in df_units.columns), None)
    
    if fuel_col and id_col:
        wind_mask = df_units[fuel_col].astype(str).str.upper() == 'WIND'
        wind_units = df_units[wind_mask][id_col].dropna().unique().tolist()
        return wind_units
    
    return []

def get_niv_data(target_date: str) -> pd.DataFrame:
    """
    Fetches Net Imbalance Volume (NIV) from Elexon Insights API.
    NIV > 0 means Short (deficit), NIV < 0 means Long (surplus).
    """
    # System prices require the date inside the URL path
    endpoint = f"{BASE_URL}/balancing/settlement/system-prices/{target_date}"
    params = {"format": "json"}
    
    response = requests.get(endpoint, params=params)
    response.raise_for_status()
    
    data = response.json().get('data', [])
    df = pd.DataFrame(data)
    
    if df.empty:
        raise ValueError(f"No system price / NIV data returned for date {target_date}")
        
    df['netImbalanceVolume'] = pd.to_numeric(df['netImbalanceVolume'])
    df['settlementPeriod'] = pd.to_numeric(df['settlementPeriod'])
    
    # Average NIV per SP to ensure exactly 48 clean rows
    df_sp = df.groupby('settlementPeriod')['netImbalanceVolume'].mean().reset_index()
    df_sp.rename(columns={'netImbalanceVolume': 'NIV'}, inplace=True)
    return df_sp

def get_wind_bids(target_date: str, wind_bmus: list) -> pd.DataFrame:
    """
    Fetches Market-wide Bid Offer Acceptances (BOALF) and aggregates 
    turn-down (bid) volume for Wind BMUs per settlement period.
    """
    # Acceptances require the 'all' endpoint for market-wide data
    # Construct URL with settlement period (1-48)
    endpoint = f"{BASE_URL}/balancing/acceptances"
    
    # Format target date as YYYY-MM-DDTHH:mm:ss for timestamp parameters
    target_timestamp = datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y-%m-%dT%H:%M:%S")
    
    params = {
        "settlementDate": target_date.replace("-", ""),  # API requires YYYYMMDD format
        "settlementPeriod": "1",  # Default to first SP, we'll aggregate all later
        "format": "json",
        "from": f"{target_timestamp}",
        "to": f"{target_timestamp.replace('T', ' ')[:16]}23:59:59",
        "BmUnit": ",".join(wind_bmus)  # Filter by our list of wind BMUs
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
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

    bmu_col = next((c for c in ['bmUnit', 'nationalGridBmUnitId', 'elexonBmUnitId'] if c in df.columns), None)
    if wind_bmus and bmu_col:
        df = df[df[bmu_col].isin(wind_bmus)]

    vol_col = next((c for c in ['acceptanceVolume', 'bidOfferVolume', 'volume'] if c in df.columns), None)
    if not vol_col:
        return pd.DataFrame({'settlementPeriod': range(1, 49), 'wind_bid_off_volume': 0.0})

    # Filter for negative acceptances (Bids / Wind turned down)
    df_bids = df[df[vol_col] < 0].copy()
    df_bids['wind_bid_off_volume'] = df_bids[vol_col].abs()
    
    sp_col = next((c for c in ['settlementPeriod', 'settlementPeriodFrom'] if c in df_bids.columns), 'settlementPeriod')
    df_bids['settlementPeriod'] = pd.to_numeric(df_bids[sp_col])
    
    aggregated = df_bids.groupby('settlementPeriod')['wind_bid_off_volume'].sum().reset_index()
    return aggregated

def calculate_adjusted_constraint_cost(target_date: str, average_offer_price: float = 75.50):
    print("Fetching Wind BMU register...")
    wind_bmus = get_wind_bmu_ids()
    print(f"Identified {len(wind_bmus)} registered wind BMUs.")

    print(f"Fetching NIV and Acceptances for {target_date}...")
    df_niv = get_niv_data(target_date)
    df_wind = get_wind_bids(target_date, wind_bmus)
    
    # Merge datasets against a master list of all 48 SPs
    all_sps = pd.DataFrame({'settlementPeriod': range(1, 49)})
    df_merged = pd.merge(all_sps, df_niv, on='settlementPeriod', how='left')
    df_merged = pd.merge(df_merged, df_wind, on='settlementPeriod', how='left')
    
    df_merged['NIV'] = df_merged['NIV'].fillna(0.0)
    df_merged['wind_bid_off_volume'] = df_merged['wind_bid_off_volume'].fillna(0.0)
    
    def calc_replacement(row):
        """Calculate adjusted replacement volume based on market state.
        
        Args:
            row: DataFrame row containing 'wind_bid_off_volume' and 'NIV'
            
        Returns:
            float: Adjusted replacement volume in MWh
            
        Logic:
        - If market is Short (NIV > 0): All bid-off wind must be replaced
        - If market is Long (NIV < 0): Only excess wind beyond market length needs replacement
        """
        vol = row['wind_bid_off_volume']
        niv = row['NIV']
        
        if vol == 0:
            return 0.0
            
        # Market is Short (NIV > 0): All bid-off wind must be replaced
        if niv > 0:
            return vol
            
        # Market is Long (NIV < 0): Only excess wind beyond market length needs replacement
        return max(0.0, vol - abs(niv))

    df_merged['adjusted_replacement_volume'] = df_merged.apply(calc_replacement, axis=1)
    df_merged['replacement_cost_gbp'] = df_merged['adjusted_replacement_volume'] * average_offer_price
    
    return df_merged

if __name__ == "__main__":
    target = '2026-11-01'
    
    daily_data = calculate_adjusted_constraint_cost(target, average_offer_price=75.50)
    
    total_cost = daily_data['replacement_cost_gbp'].sum()
    total_curtailed_mwh = daily_data['wind_bid_off_volume'].sum()
    total_adjusted_mwh = daily_data['adjusted_replacement_volume'].sum()
    
    print("\n--- RESULTS ---")
    print(f"Date: {target}")
    print(f"Total Wind Bid-Off Volume (Raw): {total_curtailed_mwh:,.2f} MWh")
    print(f"Adjusted Replacement Volume (Net Length): {total_adjusted_mwh:,.2f} MWh")
    print(f"Estimated True Constraint Replacement Cost: £{total_cost:,.2f}")
    print("\nFirst 5 Settlement Periods:")
    print(daily_data[['settlementPeriod', 'NIV', 'wind_bid_off_volume', 'adjusted_replacement_volume', 'replacement_cost_gbp']].head())
