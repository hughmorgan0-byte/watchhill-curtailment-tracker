"""
wasted_wind_calc.py
===================

Core calculation module for replicating wastedwind.energy Module 1.

Calculates the daily cost paid to curtail (switch off) wind turbines in the
GB electricity market using the public Elexon BMRS Insights Solution API.

Elexon BMRS API endpoints used
------------------------------
- ``reference/bmunits/all``
    JSON list of every Balancing Mechanism Unit with metadata including
    ``fuelType``.  Used to filter for WIND BMUs.

- ``balancing/bid-offer``
    Per-BMU bid-offer data.  Returns JSON with fields:
    ``settlementDate``, ``settlementPeriod``, ``bmUnit``, ``nationalGridBmUnit``,
    ``timeFrom``, ``timeTo``, ``levelFrom``, ``levelTo``, ``bid``, ``offer``,
    ``pairId``.

    - ``pairId < 0`` → BID (reduction).  Price is in the ``bid`` field.
    - ``pairId > 0`` → OFFER (increase).  Price is in the ``offer`` field.
    - A wind curtailment bid is identified by ``levelFrom > levelTo`` on a
      negative ``pairId``.

Sign convention (GB Balancing Mechanism)
----------------------------------------
- ``pairId < 0``  →  BID  (generator offers to reduce output)
- ``pairId > 0``  →  OFFER (generator offers to increase output)
- A **negative** bid price means the wind farm *pays* NESO to be curtailed.
- A **positive** bid price means NESO *pays* the wind farm to be curtailed
  (e.g. to recover lost CfD / ROC subsidy).
- We preserve the exact sign returned by the API.
"""

from __future__ import annotations

import time
import logging
from typing import Optional

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
API_BASE = "https://data.elexon.co.uk/bmrs/api/v1"
SETTLEMENT_PERIODS = 48
PERIOD_DURATION_HOURS = 0.5
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0
API_CALL_DELAY = 0.5  # polite delay between successive per-BMU API calls (seconds)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _get_with_retry(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> requests.Response:
    """
    Perform a GET request with basic retry / back-off for transient HTTP errors
    and rate-limit (429) responses.

    Parameters
    ----------
    url : str
        Full URL to request.
    params : dict, optional
        Query-string parameters.
    headers : dict, optional
        Additional request headers.

    Returns
    -------
    requests.Response
        The successful response object.

    Raises
    ------
    requests.HTTPError
        If all retries are exhausted.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", str(RETRY_BACKOFF_SECONDS * attempt)))
                logger.warning("Rate limited (429). Waiting %d s …", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as exc:
            last_exc = exc
            if exc.response is not None and 500 <= exc.response.status_code < 600:
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    logger.warning("Server error %s. Retrying in %d s …",
                                   exc.response.status_code, wait)
                    time.sleep(wait)
                    continue
            raise
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * attempt
                logger.warning("Connection error. Retrying in %d s …", wait)
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def _fetch_bmu_reference() -> pd.DataFrame:
    """
    Fetch the full BMU reference table from Elexon.

    Endpoint: ``GET {API_BASE}/reference/bmunits/all``
    Returns a JSON array of BMU objects.

    Returns
    -------
    pd.DataFrame
        Columns include ``elexonbmuunit``, ``nationalgridbmuunit``,
        ``bmuunitname``, ``fueltype``, etc. (lower-cased).
    """
    url = f"{API_BASE}/reference/bmunits/all"
    logger.info("Fetching BMU reference from %s", url)
    resp = _get_with_retry(url)
    data = resp.json()

    # The API may return a list directly or wrap it in a key
    if isinstance(data, dict):
        for key in ("bmunits", "data", "results"):
            if key in data:
                data = data[key]
                break

    df = pd.DataFrame(data)
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _query_bid_offer(bmu_id: str, target_date: str) -> pd.DataFrame:
    """
    Query the per-BMU bid-offer endpoint for a given settlement date.

    Endpoint: ``GET {API_BASE}/balancing/bid-offer``
    Parameters: ``bmUnit``, ``from``, ``to`` (settlement date, time ignored).

    The response JSON has a ``data`` array where each item contains:
    ``settlementDate``, ``settlementPeriod``, ``bmUnit``,
    ``nationalGridBmUnit``, ``timeFrom``, ``timeTo``,
    ``levelFrom``, ``levelTo``, ``bid``, ``offer``, ``pairId``.

    Parameters
    ----------
    bmu_id : str
        The BMU identifier (e.g. ``"T_DRAXX-1"``).
    target_date : str
        Settlement date in ``YYYY-MM-DD`` format.

    Returns
    -------
    pd.DataFrame
        One row per bid/offer pair with columns:
        ``settlementPeriod``, ``pairId``, ``levelFrom``, ``levelTo``,
        ``bid``, ``offer``, ``bmUnit``, ``nationalGridBmUnit``.
        Empty DataFrame if no data returned.
    """
    url = f"{API_BASE}/balancing/bid-offer"
    params = {
        "bmUnit": bmu_id,
        "from": target_date,
        "to": target_date,
    }

    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    # Extract the data array
    records: list[dict] = []
    if isinstance(payload, dict):
        records = payload.get("data", [])
    elif isinstance(payload, list):
        records = payload

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df.columns = [c.strip().lower() for c in df.columns]

    # Ensure numeric types
    for col in ("settlementperiod", "pairid", "levelfrom", "levelto", "bid", "offer"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def _find_column(
    df: pd.DataFrame,
    candidates: list[str],
    substring: Optional[str] = None,
) -> Optional[str]:
    """
    Find the first matching column name in a DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to search.
    candidates : list[str]
        Exact column names to try (in order).
    substring : str, optional
        If no exact match found, search for this substring in column names.

    Returns
    -------
    str or None
        The matched column name, or None.
    """
    for col in candidates:
        if col in df.columns:
            return col
    if substring:
        for col in df.columns:
            if substring in col.lower():
                return col
    return None


# ---------------------------------------------------------------------------
# Public API – the three required functions
# ---------------------------------------------------------------------------
def get_curtailed_wind_bmus(target_date: str) -> pd.DataFrame:
    """
    Identify all Wind BMUs that had a **bid accepted** (curtailed) on
    ``target_date``.

    Steps
    -----
    1. Fetch the BMU reference and filter to ``fuelType == "WIND"``.
    2. For each wind BMU, query ``balancing/bid-offer`` for the target date.
    3. Filter to rows where ``pairId < 0`` (bid) and ``levelFrom > levelTo``
       (the acceptance level moved the unit *down*, i.e. output was reduced).
    4. Return the matching BMU details together with the acceptance levels.

    Parameters
    ----------
    target_date : str
        Settlement date ``YYYY-MM-DD``.

    Returns
    -------
    pd.DataFrame
        Columns: ``elexonBmUnit``, ``bmUnitName``, ``settlementPeriod``,
        ``pairId``, ``levelFrom``, ``levelTo``, ``acceptedVolumeMW``.
    """
    empty_result = pd.DataFrame(columns=[
        "elexonBmUnit", "bmUnitName", "settlementPeriod",
        "pairId", "levelFrom", "levelTo", "acceptedVolumeMW",
    ])

    try:
        # 1 – Wind BMUs from reference data
        bmu_ref = _fetch_bmu_reference()
        if bmu_ref.empty:
            logger.error("BMU reference returned no data.")
            return empty_result

        # Identify the fuel type column
        fuel_col = _find_column(bmu_ref, ["fueltype", "fuel_type", "fuel"], substring="fuel")
        if fuel_col is None:
            logger.error("Could not identify fuel type column in BMU reference. Columns: %s",
                         list(bmu_ref.columns))
            return empty_result

        wind_bmus = bmu_ref[bmu_ref[fuel_col].astype(str).str.upper().str.strip() == "WIND"].copy()
        if wind_bmus.empty:
            logger.warning("No WIND BMUs found in reference data.")
            return empty_result

        # Identify the BMU ID column in reference (prefer elexon over nationalgrid)
        bmu_id_col_ref = _find_column(
            wind_bmus,
            ["elexonbmuunit", "elexonbmunits", "elexonbmunit"],
            substring="elexon",
        )
        if bmu_id_col_ref is None:
            bmu_id_col_ref = _find_column(wind_bmus, ["bmunit", "bmu"], substring="bmu")
        if bmu_id_col_ref is None:
            logger.error("Could not identify BMU ID column in reference. Columns: %s",
                         list(wind_bmus.columns))
            return empty_result

        wind_bmu_ids = wind_bmus[bmu_id_col_ref].astype(str).str.strip().tolist()
        logger.info("Found %d WIND BMUs in reference data.", len(wind_bmu_ids))

        # Build a name lookup
        name_col_ref = _find_column(
            wind_bmus,
            ["bmuunitname", "bmunitsname", "bmunitname", "name"],
            substring="name",
        )
        name_map: dict[str, str] = {}
        if name_col_ref:
            for _, row in wind_bmus.iterrows():
                name_map[str(row[bmu_id_col_ref]).strip()] = str(row[name_col_ref])

        # 2 – Query bid-offer per BMU and collect curtailed bids
        all_curtailed: list[pd.DataFrame] = []
        for i, bmu_id in enumerate(wind_bmu_ids):
            if i > 0:
                time.sleep(API_CALL_DELAY)

            try:
                bo_data = _query_bid_offer(bmu_id, target_date)
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.warning("Skipping BMU %s: %s", bmu_id, exc)
                continue

            if bo_data.empty:
                continue

            # Filter: bids only (pairId < 0) and level reduced (levelFrom > levelTo)
            mask = (
                (bo_data["pairid"] < 0)
                & (bo_data["levelfrom"] > bo_data["levelto"])
            )
            curtailed = bo_data[mask].copy()

            if not curtailed.empty:
                curtailed["elexonBmUnit"] = bmu_id
                all_curtailed.append(curtailed)

        if not all_curtailed:
            logger.info("No curtailed wind bids found for %s.", target_date)
            return empty_result

        # 3 – Combine all results
        combined = pd.concat(all_curtailed, ignore_index=True)

        # 4 – Build result DataFrame with consistent types
        result = pd.DataFrame({
            "elexonBmUnit": combined["elexonBmUnit"].values,
            "bmUnitName": combined["elexonBmUnit"].map(name_map).fillna("Unknown").values,
            "settlementPeriod": combined["settlementperiod"].astype(int).values,
            "pairId": combined["pairid"].astype(int).values,
            "levelFrom": combined["levelfrom"].values,
            "levelTo": combined["levelto"].values,
            "acceptedVolumeMW": (combined["levelfrom"] - combined["levelto"]).values,
        })

        logger.info("Identified %d curtailed wind bid record(s) across %d BMU(s).",
                    len(result), result["elexonBmUnit"].nunique())
        return result

    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.error("Error in get_curtailed_wind_bmus: %s", exc)
        return empty_result


def get_accepted_bid_prices(target_date: str, bmu_ids: list[str]) -> pd.DataFrame:
    """
    Retrieve the submitted bid prices (£/MWh) for the accepted wind bids.

    Queries the ``balancing/bid-offer`` endpoint for each BMU and extracts
    the ``bid`` price field (valid for ``pairId < 0``).

    Parameters
    ----------
    target_date : str
        Settlement date ``YYYY-MM-DD``.
    bmu_ids : list[str]
        Elexon BMU identifiers returned by :func:`get_curtailed_wind_bmus`.

    Returns
    -------
    pd.DataFrame
        Columns: ``elexonBmUnit``, ``settlementPeriod``, ``pairId``,
        ``price`` (£/MWh, sign preserved).
    """
    empty_result = pd.DataFrame(columns=["elexonBmUnit", "settlementPeriod", "pairId", "price"])

    try:
        all_prices: list[pd.DataFrame] = []

        for i, bmu_id in enumerate(bmu_ids):
            if i > 0:
                time.sleep(API_CALL_DELAY)

            try:
                bo_data = _query_bid_offer(bmu_id, target_date)
            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.warning("Skipping BMU %s: %s", bmu_id, exc)
                continue

            if bo_data.empty:
                continue

            # Filter to bids only (pairId < 0)
            mask = bo_data["pairid"] < 0
            bids = bo_data[mask].copy()

            if bids.empty:
                continue

            # The 'bid' column holds the price for bid pairs
            price_col = "bid" if "bid" in bids.columns else None
            if price_col is None:
                continue

            price_records = pd.DataFrame({
                "elexonBmUnit": bmu_id,
                "settlementPeriod": bids["settlementperiod"].astype(int).values,
                "pairId": bids["pairid"].astype(int).values,
                "price": bids[price_col].values,
            })
            all_prices.append(price_records)

        if not all_prices:
            logger.warning("No bid price data found for the given BMU IDs on %s.", target_date)
            return empty_result

        result = pd.concat(all_prices, ignore_index=True)
        logger.info("Retrieved %d bid price record(s).", len(result))
        return result

    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.error("Error in get_accepted_bid_prices: %s", exc)
        return empty_result


def calculate_daily_wind_bid_cost(
    acceptances: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.DataFrame:
    """
    Calculate the daily wind curtailment bid cost.

    This function is **decoupled** from any API logic.  It receives two
    DataFrames (acceptance volumes and prices) and performs the arithmetic:

    1. Merge acceptances with prices on BMU + period + pairId.
    2. Convert accepted MW to MWh (× 0.5 h per settlement period).
    3. Compute cost = MWh × price (£/MWh) per record.
    4. Aggregate per BMU: total curtailed MWh, average price, total cost.
    5. Return a per-BMU summary DataFrame.

    Parameters
    ----------
    acceptances : pd.DataFrame
        Output of :func:`get_curtailed_wind_bmus`.
        Must contain: ``elexonBmUnit``, ``settlementPeriod``, ``pairId``,
        ``acceptedVolumeMW``, ``bmUnitName``.
    prices : pd.DataFrame
        Output of :func:`get_accepted_bid_prices`.
        Must contain: ``elexonBmUnit``, ``settlementPeriod``, ``pairId``,
        ``price``.

    Returns
    -------
    pd.DataFrame
        One row per BMU with columns:
        ``elexonBmUnit``, ``bmUnitName``, ``curtailed_mwh``,
        ``avg_price``, ``total_cost``.
    """
    empty_cols = ["elexonBmUnit", "bmUnitName", "curtailed_mwh", "avg_price", "total_cost"]
    if acceptances.empty or prices.empty:
        logger.warning("Empty input data – returning empty result.")
        return pd.DataFrame(columns=empty_cols)

    # Ensure merge keys are consistently typed
    for df in (acceptances, prices):
        df["elexonBmUnit"] = df["elexonBmUnit"].astype(str).str.strip()
        df["settlementPeriod"] = pd.to_numeric(df["settlementPeriod"], errors="coerce").astype(int)
        df["pairId"] = pd.to_numeric(df["pairId"], errors="coerce").astype(int)

    # Merge on the three key columns
    merged = acceptances.merge(
        prices,
        on=["elexonBmUnit", "settlementPeriod", "pairId"],
        how="inner",
    )

    if merged.empty:
        logger.warning(
            "No matching records after merge. "
            "Acceptances: %d rows, Prices: %d rows.",
            len(acceptances), len(prices),
        )
        return pd.DataFrame(columns=empty_cols)

    # Convert MW → MWh (each settlement period is 30 minutes)
    merged["curtailed_mwh"] = merged["acceptedVolumeMW"] * PERIOD_DURATION_HOURS

    # Cost for each record: MWh × £/MWh
    merged["record_cost"] = merged["curtailed_mwh"] * merged["price"]

    # Aggregate per BMU
    summary = (
        merged
        .groupby(["elexonBmUnit", "bmUnitName"], as_index=False)
        .agg(
            curtailed_mwh=("curtailed_mwh", "sum"),
            total_cost=("record_cost", "sum"),
        )
    )
    summary["avg_price"] = summary["total_cost"] / summary["curtailed_mwh"].replace(0, pd.NA)
    summary["avg_price"] = summary["avg_price"].fillna(0.0)

    # Sort by total cost descending for the summary table
    summary = summary.sort_values("total_cost", ascending=False).reset_index(drop=True)

    logger.info(
        "Daily totals – %d BMU(s), %.1f MWh curtailed, £%.2f total cost.",
        len(summary),
        summary["curtailed_mwh"].sum(),
        summary["total_cost"].sum(),
    )
    return summary
