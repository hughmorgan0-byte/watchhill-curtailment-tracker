"""
wasted_wind_calc.py
===================

Core calculation module for replicating wastedwind.energy Module 1.

Calculates the daily cost paid to curtail (switch off) wind turbines in the
GB electricity market using the public Elexon BMRS Insights Solution API.

Elexon BMRS API endpoints used
------------------------------
- ``GET /reference/bmunits/all``
    Returns a JSON array of all Balancing Mechanism Units.
    Key fields: ``elexonBmUnit``, ``nationalGridBmUnit``, ``bmUnitName``,
    ``fuelType``, ``leadPartyName``, ``generationCapacity``, etc.

- ``GET /datasets/BOALF?from=...&to=...``
    Bid-Offer Acceptance Level Flagged data.
    Query params: ``from`` (RFC 3339 date-time, required),
                  ``to``   (RFC 3339 date-time, required),
                  ``settlementPeriodFrom`` (int 1-50, optional),
                  ``settlementPeriodTo``   (int 1-50, optional),
                  ``bmUnit`` (string array, optional),
                  ``format`` (json|xml|csv, optional).
    Response: ``{"data": [ ... ]}`` where each item has:
    ``settlementDate``, ``settlementPeriodFrom``, ``settlementPeriodTo``,
    ``timeFrom``, ``timeTo``, ``levelFrom``, ``levelTo``,
    ``acceptanceNumber``, ``acceptanceTime``, ``deemedBoFlag``,
    ``soFlag``, ``amendmentFlag``, ``storFlag``, ``rrFlag``,
    ``nationalGridBmUnit``, ``bmUnit``.

- ``GET /datasets/BOD?from=...&to=...``
    Bid-Offer Data (submitted bids and offers with prices).
    Same query params as BOALF.
    Response: ``{"data": [ ... ]}`` where each item has:
    ``dataset``, ``settlementDate``, ``settlementPeriod``,
    ``timeFrom``, ``levelFrom``, ``timeTo``, ``levelTo``,
    ``pairId``, ``offer``, ``bid``, ``nationalGridBmUnit``, ``bmUnit``.

    - ``pairId < 0`` → BID (reduction).  Price in ``bid`` field.
    - ``pairId > 0`` → OFFER (increase).  Price in ``offer`` field.

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
API_CALL_DELAY = 1.0  # polite delay between successive API calls (seconds)

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
            resp = requests.get(url, params=params, headers=headers, timeout=60)
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
    Returns a JSON array of BMU objects with camelCase fields.

    Returns
    -------
    pd.DataFrame
        Columns (original camelCase preserved):
        ``elexonBmUnit``, ``nationalGridBmUnit``, ``bmUnitName``,
        ``fuelType``, ``leadPartyName``, ``generationCapacity``, etc.
    """
    url = f"{API_BASE}/reference/bmunits/all"
    logger.info("Fetching BMU reference from %s", url)
    resp = _get_with_retry(url)
    data = resp.json()

    # The API returns a JSON array directly per the schema
    if isinstance(data, dict):
        for key in ("data", "bmunits", "results"):
            if key in data:
                data = data[key]
                break

    df = pd.DataFrame(data)
    if df.empty:
        return df

    # Ensure string columns are stripped
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()

    return df


def _fetch_bod(target_date: str, bmu_ids: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Fetch Bid-Offer Data (BOD) for a given settlement date.

    Endpoint: ``GET {API_BASE}/datasets/BOD``
    Parameters:
        - ``from``: RFC 3339 date-time (settlement date, time ignored)
        - ``to``:   RFC 3339 date-time (settlement date, time ignored)
        - ``bmUnit``: optional array of BMU IDs to filter
        - ``format``: ``json``

    Response: ``{"data": [ ... ]}``

    Parameters
    ----------
    target_date : str
        Settlement date in ``YYYY-MM-DD`` format.
    bmu_ids : list[str], optional
        If provided, only return data for these BMUs.

    Returns
    -------
    pd.DataFrame
        Columns: ``settlementDate``, ``settlementPeriod``, ``timeFrom``,
        ``levelFrom``, ``timeTo``, ``levelTo``, ``pairId``, ``offer``,
        ``bid``, ``nationalGridBmUnit``, ``bmUnit``.
        Empty DataFrame if no data returned.
    """
    url = f"{API_BASE}/datasets/BOD"
    params: dict = {
        "from": f"{target_date}T00:00:00Z",
        "to": f"{target_date}T00:00:00Z",
        "format": "json",
    }
    if bmu_ids:
        params["bmUnit"] = bmu_ids

    logger.info("Fetching BOD for %s (%d BMU filter%s)",
                target_date, len(bmu_ids) if bmu_ids else 0,
                "" if bmu_ids else "s – all")
    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    # Unwrap the response envelope
    if isinstance(payload, dict):
        records = payload.get("data", [])
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    if not records:
        logger.warning("BOD returned no data for %s.", target_date)
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # Ensure numeric types
    for col in ("settlementPeriod", "levelFrom", "levelTo", "pairId", "bid", "offer"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Ensure string types
    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    return df


def _fetch_boalf(target_date: str, bmu_ids: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Fetch Bid-Offer Acceptance Level Flagged (BOALF) data for a given date.

    Endpoint: ``GET {API_BASE}/datasets/BOALF``
    Parameters: same as BOD.

    Response: ``{"data": [ ... ]}``

    Parameters
    ----------
    target_date : str
        Settlement date in ``YYYY-MM-DD`` format.
    bmu_ids : list[str], optional
        If provided, only return data for these BMUs.

    Returns
    -------
    pd.DataFrame
        Columns: ``settlementDate``, ``settlementPeriodFrom``,
        ``settlementPeriodTo``, ``timeFrom``, ``timeTo``, ``levelFrom``,
        ``levelTo``, ``acceptanceNumber``, ``acceptanceTime``,
        ``deemedBoFlag``, ``soFlag``, ``amendmentFlag``, ``storFlag``,
        ``rrFlag``, ``nationalGridBmUnit``, ``bmUnit``.
        Empty DataFrame if no data returned.
    """
    url = f"{API_BASE}/datasets/BOALF"
    params: dict = {
        "from": f"{target_date}T00:00:00Z",
        "to": f"{target_date}T00:00:00Z",
        "format": "json",
    }
    if bmu_ids:
        params["bmUnit"] = bmu_ids

    logger.info("Fetching BOALF for %s", target_date)
    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    if isinstance(payload, dict):
        records = payload.get("data", [])
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    if not records:
        logger.warning("BOALF returned no data for %s.", target_date)
        return pd.DataFrame()

    df = pd.DataFrame(records)

    for col in ("settlementPeriodFrom", "settlementPeriodTo", "levelFrom", "levelTo",
                "acceptanceNumber"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    return df


# ---------------------------------------------------------------------------
# Public API – the three required functions
# ---------------------------------------------------------------------------
def get_curtailed_wind_bmus(target_date: str) -> pd.DataFrame:
    """
    Identify all Wind BMUs that had a **bid accepted** (curtailed) on
    ``target_date``.

    Steps
    -----
    1. Fetch the BMU reference (``/reference/bmunits/all``) and filter to
       ``fuelType == "WIND"``.
    2. Fetch BOD data (``/datasets/BOD``) for the target date.
    3. Filter to wind BMUs with ``pairId < 0`` (bid) – these represent
       curtailment bids submitted to the market.
    4. The accepted volume is derived from the bid levels.

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
        # 1 – Get wind BMUs from reference data
        bmu_ref = _fetch_bmu_reference()
        if bmu_ref.empty:
            logger.error("BMU reference returned no data.")
            return empty_result

        if "fuelType" not in bmu_ref.columns:
            logger.error("BMU reference missing 'fuelType' column. Columns: %s",
                         list(bmu_ref.columns))
            return empty_result

        wind_bmus = bmu_ref[bmu_ref["fuelType"].str.upper() == "WIND"].copy()
        if wind_bmus.empty:
            logger.warning("No WIND BMUs found in reference data.")
            return empty_result

        if "elexonBmUnit" not in wind_bmus.columns:
            logger.error("BMU reference missing 'elexonBmUnit' column.")
            return empty_result

        wind_bmu_ids = wind_bmus["elexonBmUnit"].tolist()
        logger.info("Found %d WIND BMUs in reference data.", len(wind_bmu_ids))

        # Build name lookup
        name_map: dict[str, str] = {}
        if "bmUnitName" in wind_bmus.columns:
            for _, row in wind_bmus.iterrows():
                name_map[row["elexonBmUnit"]] = row["bmUnitName"]

        # 2 – Fetch BOD data for the target date (all BMUs, filter client-side)
        time.sleep(API_CALL_DELAY)
        bod = _fetch_bod(target_date)
        if bod.empty:
            logger.warning("BOD returned no data for %s.", target_date)
            return empty_result

        # 3 – Filter to wind BMUs with bids (pairId < 0)
        if "bmUnit" not in bod.columns:
            logger.error("BOD data missing 'bmUnit' column. Columns: %s",
                         list(bod.columns))
            return empty_result

        mask = (
            (bod["bmUnit"].isin(wind_bmu_ids))
            & (bod["pairId"] < 0)
        )
        wind_bids = bod[mask].copy()

        if wind_bids.empty:
            logger.info("No wind bids found in BOD for %s.", target_date)
            return empty_result

        # 4 – Build result DataFrame
        # Volume: the absolute MW level of the bid (negative levels = reduction)
        # If levelFrom and levelTo differ, use the absolute difference as volume
        vol_from = wind_bids["levelFrom"].abs()
        vol_to = wind_bids["levelTo"].abs()
        # If both are the same (flat bid), volume = that value
        # If they differ, volume = abs(levelFrom - levelTo)
        volume = (vol_from - vol_to).abs()
        # If volume is 0 (levelFrom == levelTo), use the absolute level
        zero_mask = volume == 0
        volume[zero_mask] = vol_from[zero_mask]

        result = pd.DataFrame({
            "elexonBmUnit": wind_bids["bmUnit"].values,
            "bmUnitName": wind_bids["bmUnit"].map(name_map).fillna("Unknown").values,
            "settlementPeriod": wind_bids["settlementPeriod"].astype(int).values,
            "pairId": wind_bids["pairId"].astype(int).values,
            "levelFrom": wind_bids["levelFrom"].values,
            "levelTo": wind_bids["levelTo"].values,
            "acceptedVolumeMW": volume.values,
        })

        logger.info("Identified %d wind bid record(s) across %d BMU(s).",
                    len(result), result["elexonBmUnit"].nunique())
        return result

    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.error("Error in get_curtailed_wind_bmus: %s", exc)
        return empty_result


def get_accepted_bid_prices(target_date: str, bmu_ids: list[str]) -> pd.DataFrame:
    """
    Retrieve the bid prices (£/MWh) for the accepted wind bids.

    Queries the ``/datasets/BOD`` endpoint filtered to the given BMU IDs
    and extracts the ``bid`` price field for ``pairId < 0`` records.

    Parameters
    ----------
    target_date : str
        Settlement date ``YYYY-MM-DD``.
    bmu_ids : list[str]
        Elexon BMU identifiers (``elexonBmUnit``) from
        :func:`get_curtailed_wind_bmus`.

    Returns
    -------
    pd.DataFrame
        Columns: ``elexonBmUnit``, ``settlementPeriod``, ``pairId``,
        ``price`` (£/MWh, sign preserved).
    """
    empty_result = pd.DataFrame(columns=["elexonBmUnit", "settlementPeriod", "pairId", "price"])

    try:
        time.sleep(API_CALL_DELAY)
        bod = _fetch_bod(target_date, bmu_ids=bmu_ids)
        if bod.empty:
            logger.warning("BOD returned no data for %s with BMU filter.", target_date)
            return empty_result

        if "bmUnit" not in bod.columns or "pairId" not in bod.columns:
            logger.error("BOD data missing required columns. Columns: %s",
                         list(bod.columns))
            return empty_result

        # Filter to bids only (pairId < 0)
        mask = (
            (bod["bmUnit"].isin(bmu_ids))
            & (bod["pairId"] < 0)
        )
        bids = bod[mask].copy()

        if bids.empty:
            logger.warning("No bid records found for the given BMU IDs on %s.", target_date)
            return empty_result

        if "bid" not in bids.columns:
            logger.error("BOD data missing 'bid' price column. Columns: %s",
                         list(bids.columns))
            return empty_result

        result = pd.DataFrame({
            "elexonBmUnit": bids["bmUnit"].values,
            "settlementPeriod": bids["settlementPeriod"].astype(int).values,
            "pairId": bids["pairId"].astype(int).values,
            "price": bids["bid"].values,
        })

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
