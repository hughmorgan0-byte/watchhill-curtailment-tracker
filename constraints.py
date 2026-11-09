def get_wind_bids(target_date: str, wind_bmus: list) -> pd.DataFrame:
    """
    Fetches Market-wide Bid Acceptance Volumes (BOAV) and aggregates 
    turn-down (bid) volume for Wind BMUs per settlement period.
    """
    # Use the explicit Acceptance Volumes (BOAV) endpoint for Bids
    # This requires YYYY-MM-DD format
    endpoint = f"{BASE_URL}/balancing/settlement/acceptance/volumes/all/Bid/{target_date}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    # We fetch ALL bids for the date to avoid URL length limits with hundreds of BMUs
    response = requests.get(endpoint, params={"format": "json"}, headers=headers)
    
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"Error {response.status_code}: {response.text}\nRequest URL: {response.url}"
        )
    
    data = response.json().get('data', [])
    df = pd.DataFrame(data)
    
    if df.empty:
        return pd.DataFrame({'settlementPeriod': range(1, 49), 'wind_bid_off_volume': 0.0})

    # Filter the market data against our known Wind BMUs
    bmu_col = next((c for c in ['bmUnit', 'nationalGridBmUnitId', 'elexonBmUnitId'] if c in df.columns), 'bmUnit')
    df = df[df[bmu_col].isin(wind_bmus)].copy()

    # The BOAV endpoint provides pre-calculated 'acceptanceVolume' in MWh
    vol_col = next((c for c in ['acceptanceVolume', 'volume'] if c in df.columns), 'acceptanceVolume')
    if vol_col not in df.columns:
        return pd.DataFrame({'settlementPeriod': range(1, 49), 'wind_bid_off_volume': 0.0})

    # Bids are returned as negative volumes, convert to absolute turn-down volume
    df['wind_bid_off_volume'] = df[vol_col].abs()
    
    sp_col = next((c for c in ['settlementPeriod', 'settlementPeriodFrom'] if c in df.columns), 'settlementPeriod')
    df['settlementPeriod'] = pd.to_numeric(df[sp_col])
    
    # Aggregate total volume per settlement period
    aggregated = df.groupby('settlementPeriod')['wind_bid_off_volume'].sum().reset_index()
    
    return aggregated