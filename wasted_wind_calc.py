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

- ``GET /balancing/settlement/acceptance/volumes/all/bid/{settlementDate}``
    Accepted bid volumes for a given settlement date.
    ``settlementDate`` is a path parameter in ``YYYY-MM-DD`` format.
    Optional query params: ``bmUnit`` (string array).
    Response: a JSON array where each item has:
    ``createdDateTime``, ``settlementDate``, ``settlementPeriod``,
    ``startTime``, ``bmUnit``, ``bmUnitType``, ``leadPartyName``,
    ``nationalGridBmUnit``, ``acceptanceId``, ``acceptanceDuration``,
    ``totalVolumeAccepted``, ``pairVolumes``.

    ``pairVolumes`` is a nested object:
        ``negative1`` … ``negative6``  – accepted bid volumes (MWh, negative)
        ``positive1`` … ``positive6``  – accepted offer volumes (MWh, positive)

    Only ``negative{N}`` values represent curtailment (bid accepted).

- ``GET /datasets/BOD/stream?from=...&to=...``
    Bid-Offer Data (submitted bid/offer prices) – streaming endpoint.
    Query params: ``from`` (RFC 3339 date-time, required),
                  ``to``   (RFC 3339 date-time, required).
    Response: a raw JSON array ``[ ... ]`` (NOT wrapped in ``{"data": [...]}``).
    Each item has:
    ``dataset``, ``settlementDate``, ``settlementPeriod``,
    ``timeFrom``, ``levelFrom``, ``timeTo``, ``levelTo``,
    ``pairId``, ``offer``, ``bid``, ``nationalGridBmUnit``, ``bmUnit``.

    - ``pairId < 0`` → BID (reduction).  Price in ``bid`` field.
    - ``pairId > 0`` → OFFER (increase).  Price in ``offer`` field.

- ``GET /datasets/BOALF/stream?from=...&to=...``
    Bid-Offer Acceptance Level Flagged – streaming endpoint.
    Query params: ``from`` (RFC 3339 date-time, required),
                  ``to``   (RFC 3339 date-time, required).
    Response: a raw JSON array ``[ ... ]``.
    Each item has:
    ``dataset``, ``settlementDate``, ``settlementPeriodFrom``,
    ``settlementPeriodTo``, ``timeFrom``, ``timeTo``, ``levelFrom``,
    ``levelTo``, ``acceptanceNumber``, ``acceptanceTime``,
    ``deemedBoFlag``, ``soFlag``, ``amendmentFlag``, ``storFlag``,
    ``rrFlag``, ``nationalGridBmUnit``, ``bmUnit``.

    - ``soFlag == True`` indicates a System Operator (constraint-related) action.
    - ``acceptanceNumber`` corresponds to BOAV's ``acceptanceId``.

- ``GET /datasets/EBOCF?from=...&to=...``
    Elexon's official Indicative Period BMU Cashflows.
    Used for cross-checking our calculated totals.

Sign convention (GB Balancing Mechanism cashflow)
-------------------------------------------------
- BOAV ``negative{N}`` volume is reported as a **negative** MWh value
  (e.g. ``-12.5`` MWh accepted at pair -1).
- BOD ``bid`` price preserves its sign:
  - Negative price (e.g. ``-£85/MWh``): wind farm pays NESO to curtail.
  - Positive price (e.g. ``+£20/MWh``): NESO pays wind farm to curtail
    (e.g. to recover lost CfD/ROC subsidy).
- Cashflow: ``cost = volume × price``
  - ``-12.5 × -85 = +£1062.50``  → wind farm pays NESO (saving to system).
  - ``-12.5 × +20 = -£250.00``   → NESO pays wind farm (cost to system).
- For reporting: ``curtailed_mwh = abs(volume)`` (always positive).
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timedelta
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
            resp = requests.get(url, params=params, headers=headers, timeout=120)
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


def _fetch_boav_bid(target_date: str, bmu_ids: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Fetch accepted bid volumes (BOAV) for a given settlement date.

    Endpoint: ``GET {API_BASE}/balancing/settlement/acceptance/volumes/all/bid/{settlementDate}``
    The ``settlementDate`` is a path parameter in ``YYYY-MM-DD`` format.
    Optional query param: ``bmUnit`` (array of BMU IDs).

    Response: a JSON array where each item contains a nested ``pairVolumes``
    object with ``negative1``–``negative6`` and ``positive1``–``positive6``.

    This helper flattens ``pairVolumes`` into top-level columns.

    Parameters
    ----------
    target_date : str
        Settlement date in ``YYYY-MM-DD`` format.
    bmu_ids : list[str], optional
        If provided, only return data for these BMUs.

    Returns
    -------
    pd.DataFrame
        Columns: ``settlementDate``, ``settlementPeriod``, ``startTime``,
        ``bmUnit``, ``bmUnitType``, ``leadPartyName``,
        ``nationalGridBmUnit``, ``acceptanceId``, ``acceptanceDuration``,
        ``totalVolumeAccepted``, ``negative1``–``negative6``,
        ``positive1``–``positive6``.
        Empty DataFrame if no data returned.
    """
    url = f"{API_BASE}/balancing/settlement/acceptance/volumes/all/bid/{target_date}"
    params: dict = {}
    if bmu_ids:
        params["bmUnit"] = bmu_ids

    logger.info("Fetching BOAV bid for %s (%d BMU filter%s)",
                target_date, len(bmu_ids) if bmu_ids else 0,
                "" if bmu_ids else "s – all")
    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    # The response is a bare JSON array (per boav_bid_sample.json)
    # but handle the wrapped case too
    if isinstance(payload, dict):
        records = payload.get("data", [])
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    if not records:
        logger.warning("BOAV bid returned no data for %s.", target_date)
        return pd.DataFrame()

    # Flatten the nested pairVolumes object into top-level columns
    flat_records: list[dict] = []
    for rec in records:
        pair_vols = rec.pop("pairVolumes", None) or {}
        for n in range(1, 7):
            rec[f"negative{n}"] = pair_vols.get(f"negative{n}")
            rec[f"positive{n}"] = pair_vols.get(f"positive{n}")
        flat_records.append(rec)

    df = pd.DataFrame(flat_records)

    # Ensure numeric types
    numeric_cols = (
        ["settlementPeriod", "acceptanceId", "totalVolumeAccepted"]
        + [f"negative{n}" for n in range(1, 7)]
        + [f"positive{n}" for n in range(1, 7)]
    )
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Ensure string types
    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate",
                "bmUnitType", "leadPartyName", "acceptanceDuration"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    return df


def _fetch_bod_stream(target_date: str) -> pd.DataFrame:
    """
    Fetch Bid-Offer Data (BOD) for a given settlement date using the
    streaming endpoint to avoid dataset size limits.

    Endpoint: ``GET {API_BASE}/datasets/BOD/stream``
    Parameters:
        - ``from``: RFC 3339 date-time (start of settlement day, e.g. ``2026-11-17T00:00Z``)
        - ``to``:   RFC 3339 date-time (end of settlement day, e.g. ``2026-11-18T00:00Z``)

    Note: The ``/stream`` endpoint returns a raw JSON array ``[ ... ]``
    directly (NOT wrapped in ``{"data": [...]}``).  This avoids the 400
    Bad Request that the non-streaming ``/datasets/BOD`` endpoint returns
    for full-day unfiltered queries due to Elexon's dataset size limit.

    Parameters
    ----------
    target_date : str
        Settlement date in ``YYYY-MM-DD`` format.

    Returns
    -------
    pd.DataFrame
        Columns: ``settlementDate``, ``settlementPeriod``, ``timeFrom``,
        ``levelFrom``, ``timeTo``, ``levelTo``, ``pairId``, ``offer``,
        ``bid``, ``nationalGridBmUnit``, ``bmUnit``.
        Empty DataFrame if no data returned.
    """
    url = f"{API_BASE}/datasets/BOD/stream"

    # Cover the full settlement day using the API's expected date format (no seconds)
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    next_dt = dt + timedelta(days=1)
    from_str = dt.strftime("%Y-%m-%dT00:00Z")
    to_str = next_dt.strftime("%Y-%m-%dT00:00Z")

    params: dict = {
        "from": from_str,
        "to": to_str,
    }

    logger.info("Fetching BOD stream for %s (full day, filtering locally)",
                target_date)
    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    # The /stream endpoint returns a raw JSON array directly
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        # Handle the wrapped case just in case
        records = payload.get("data", [])
    else:
        records = []

    if not records:
        logger.warning("BOD stream returned no data for %s.", target_date)
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

    logger.info("BOD stream returned %d total record(s) for %s.", len(df), target_date)
    return df


def _fetch_boalf_stream(target_date: str) -> pd.DataFrame:
    """
    Fetch Bid-Offer Acceptance Level Flagged (BOALF) data for a given
    settlement date using the streaming endpoint.

    Endpoint: ``GET {API_BASE}/datasets/BOALF/stream``
    Parameters:
        - ``from``: RFC 3339 date-time (start of settlement day)
        - ``to``:   RFC 3339 date-time (end of settlement day)

    Response: a raw JSON array ``[ ... ]``.
    Each item has:
    ``dataset``, ``settlementDate``, ``settlementPeriodFrom``,
    ``settlementPeriodTo``, ``timeFrom``, ``timeTo``, ``levelFrom``,
    ``levelTo``, ``acceptanceNumber``, ``acceptanceTime``,
    ``deemedBoFlag``, ``soFlag``, ``amendmentFlag``, ``storFlag``,
    ``rrFlag``, ``nationalGridBmUnit``, ``bmUnit``.

    Parameters
    ----------
    target_date : str
        Settlement date in ``YYYY-MM-DD`` format.

    Returns
    -------
    pd.DataFrame
        Columns: ``settlementDate``, ``settlementPeriodFrom``,
        ``settlementPeriodTo``, ``levelFrom``, ``levelTo``,
        ``acceptanceNumber``, ``soFlag``, ``deemedBoFlag``,
        ``bmUnit``, ``nationalGridBmUnit``, etc.
        Empty DataFrame if no data returned.
    """
    url = f"{API_BASE}/datasets/BOALF/stream"

    dt = datetime.strptime(target_date, "%Y-%m-%d")
    next_dt = dt + timedelta(days=1)
    from_str = dt.strftime("%Y-%m-%dT00:00Z")
    to_str = next_dt.strftime("%Y-%m-%dT00:00Z")

    params: dict = {
        "from": from_str,
        "to": to_str,
    }

    logger.info("Fetching BOALF stream for %s", target_date)
    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    # The /stream endpoint returns a raw JSON array directly
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("data", [])
    else:
        records = []

    if not records:
        logger.warning("BOALF stream returned no data for %s.", target_date)
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # Ensure numeric types
    for col in ("settlementPeriodFrom", "settlementPeriodTo",
                "levelFrom", "levelTo", "acceptanceNumber"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Ensure boolean types
    for col in ("soFlag", "deemedBoFlag", "storFlag", "rrFlag"):
        if col in df.columns:
            df[col] = df[col].astype(bool)

    # Ensure string types
    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    logger.info("BOALF stream returned %d record(s) for %s.", len(df), target_date)
    return df


def _fetch_ebocf(target_date: str, bmu_ids: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Fetch Elexon's official Indicative Period BMU Cashflows (EBOCF) for cross-checking.

    Endpoint: ``GET {API_BASE}/datasets/EBOCF``
    Parameters:
        - ``from``: RFC 3339 date-time
        - ``to``:   RFC 3339 date-time
        - ``format``: ``json``

    Response: ``{"data": [ ... ]}`` where each item has:
    ``settlementDate``, ``settlementPeriod``, ``startTime``,
    ``bmUnit``, ``bmUnitType``, ``leadPartyName``,
    ``nationalGridBmUnit``, ``bidOfferPairCashflows``, ``totalCashflow``.

    Parameters
    ----------
    target_date : str
        Settlement date in ``YYYY-MM-DD`` format.
    bmu_ids : list[str], optional
        If provided, filter locally to these BMUs.

    Returns
    -------
    pd.DataFrame
        Flattened EBOCF data. Empty DataFrame if unavailable.
    """
    url = f"{API_BASE}/datasets/EBOCF"

    dt = datetime.strptime(target_date, "%Y-%m-%d")
    next_dt = dt + timedelta(days=1)
    from_str = dt.strftime("%Y-%m-%dT00:00Z")
    to_str = next_dt.strftime("%Y-%m-%dT00:00Z")

    params: dict = {
        "from": from_str,
        "to": to_str,
        "format": "json",
    }

    try:
        logger.info("Fetching EBOCF for %s (cross-check)", target_date)
        resp = _get_with_retry(url, params=params)
        payload = resp.json()

        if isinstance(payload, dict):
            records = payload.get("data", [])
        elif isinstance(payload, list):
            records = payload
        else:
            records = []

        if not records:
            logger.info("EBOCF returned no data for %s.", target_date)
            return pd.DataFrame()

        # Flatten bidOfferPairCashflows
        flat_records: list[dict] = []
        for rec in records:
            cashflows = rec.pop("bidOfferPairCashflows", None) or {}
            for n in range(1, 7):
                rec[f"cashflow_negative{n}"] = cashflows.get(f"negative{n}")
                rec[f"cashflow_positive{n}"] = cashflows.get(f"positive{n}")
            flat_records.append(rec)

        df = pd.DataFrame(flat_records)

        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str).str.strip()
            else:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Filter locally if bmu_ids provided
        if bmu_ids and "bmUnit" in df.columns:
            df = df[df["bmUnit"].isin(bmu_ids)].reset_index(drop=True)

        logger.info("EBOCF returned %d record(s) for cross-check.", len(df))
        return df

    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("Could not fetch EBOCF for cross-check: %s", exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Public API – the three required functions
# ---------------------------------------------------------------------------
def get_curtailed_wind_bmus(target_date: str) -> pd.DataFrame:
    """
    Identify all Wind BMUs that had an **accepted bid** (curtailed) on
    ``target_date``.

    Steps
    -----
    1. Fetch the BMU reference (``/reference/bmunits/all``) and filter to
       ``fuelType == "WIND"``.
    2. Fetch accepted bid volumes from
       ``/balancing/settlement/acceptance/volumes/all/bid/{target_date}``.
    3. For each record, check ``negative1`` through ``negative6``
       (accepted bid volumes, reported as negative MWh).
    4. Only include records where at least one ``negative{N}`` is non-zero.
       Do NOT include ``positive{N}`` (offers/turn-ups).
    5. Expand into one row per non-zero negative pair volume.
    6. Fetch BOALF and tag each record with ``is_system_flag`` based on
       ``soFlag`` (matching ``acceptanceId`` to ``acceptanceNumber``).

    Parameters
    ----------
    target_date : str
        Settlement date ``YYYY-MM-DD``.

    Returns
    -------
    pd.DataFrame
        Columns: ``elexonBmUnit``, ``bmUnitName``, ``settlementPeriod``,
        ``pairId`` (negative, e.g. -1, -2), ``acceptedVolumeMWh``
        (negative value, e.g. -12.5), ``is_system_flag`` (bool).
    """
    empty_result = pd.DataFrame(columns=[
        "elexonBmUnit", "bmUnitName", "settlementPeriod",
        "pairId", "acceptedVolumeMWh", "is_system_flag",
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

        wind_bmu_ids = set(wind_bmus["elexonBmUnit"].tolist())
        logger.info("Found %d WIND BMUs in reference data.", len(wind_bmu_ids))

        # Build name lookup
        name_map: dict[str, str] = {}
        if "bmUnitName" in wind_bmus.columns:
            for _, row in wind_bmus.iterrows():
                name_map[row["elexonBmUnit"]] = row["bmUnitName"]

        # 2 – Fetch accepted bid volumes (BOAV bid) for the target date
        time.sleep(API_CALL_DELAY)
        boav = _fetch_boav_bid(target_date)
        if boav.empty:
            logger.warning("BOAV bid returned no data for %s.", target_date)
            return empty_result

        # 3 – Filter to wind BMUs
        if "bmUnit" not in boav.columns:
            logger.error("BOAV data missing 'bmUnit' column. Columns: %s",
                         list(boav.columns))
            return empty_result

        wind_boav = boav[boav["bmUnit"].isin(wind_bmu_ids)].copy()
        if wind_boav.empty:
            logger.info("No BOAV bid records for wind BMUs on %s.", target_date)
            return empty_result

        # 4 – Expand: one row per non-zero negative{N} pair volume
        rows: list[dict] = []
        for _, rec in wind_boav.iterrows():
            bmu = rec["bmUnit"]
            period = int(rec["settlementPeriod"])
            name = name_map.get(bmu, "Unknown")
            acceptance_id = rec.get("acceptanceId")

            for n in range(1, 7):
                vol = rec.get(f"negative{n}")
                if vol is not None and not pd.isna(vol) and vol != 0:
                    rows.append({
                        "elexonBmUnit": bmu,
                        "bmUnitName": name,
                        "settlementPeriod": period,
                        "pairId": -n,
                        "acceptedVolumeMWh": float(vol),  # negative value
                        "acceptanceId": acceptance_id,
                    })

        if not rows:
            logger.info("No accepted bid volumes found for wind BMUs on %s.", target_date)
            return empty_result

        result = pd.DataFrame(rows)

        # 5 – Fetch BOALF and tag with soFlag
        time.sleep(API_CALL_DELAY)
        boalf = _fetch_boalf_stream(target_date)

        if not boalf.empty and "acceptanceNumber" in boalf.columns and "soFlag" in boalf.columns:
            # Build a set of (bmUnit, acceptanceNumber) where soFlag is True
            system_flags = set()
            for _, boalf_row in boalf.iterrows():
                if boalf_row.get("soFlag") is True:
                    bmu_key = str(boalf_row.get("bmUnit", "")).strip()
                    acc_num = boalf_row.get("acceptanceNumber")
                    if bmu_key and acc_num is not None and not pd.isna(acc_num):
                        system_flags.add((bmu_key, int(acc_num)))

            # Tag each row
            is_system = []
            for _, row in result.iterrows():
                bmu = row["elexonBmUnit"]
                acc_id = row.get("acceptanceId")
                if acc_id is not None and not pd.isna(acc_id):
                    is_system.append((bmu, int(acc_id)) in system_flags)
                else:
                    is_system.append(False)

            result["is_system_flag"] = is_system
            n_system = sum(is_system)
            logger.info(
                "BOALF soFlag: %d/%d records are system-related (soFlag=True).",
                n_system, len(result),
            )
        else:
            # BOALF unavailable – default all to False
            result["is_system_flag"] = False
            logger.warning(
                "BOALF data unavailable or missing soFlag/acceptanceNumber. "
                "Defaulting all records to is_system_flag=False."
            )

        # Drop the temporary acceptanceId column
        result = result.drop(columns=["acceptanceId"], errors="ignore")

        logger.info("Identified %d accepted bid record(s) across %d BMU(s).",
                    len(result), result["elexonBmUnit"].nunique())
        return result

    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.error("Error in get_curtailed_wind_bmus: %s", exc)
        return empty_result


def get_accepted_bid_prices(target_date: str, bmu_ids: list[str]) -> pd.DataFrame:
    """
    Retrieve the bid prices (£/MWh) for the accepted wind bids.

    Uses the ``/datasets/BOD/stream`` endpoint (streaming) to avoid the
    400 Bad Request that the non-streaming endpoint returns for full-day
    unfiltered queries.  The stream endpoint returns a raw JSON array.

    Filters locally to the given BMU IDs and ``pairId < 0`` (bids).

    The ``pairId`` in BOD corresponds directly to the BOAV pair number:
    BOAV ``negative1`` ↔ BOD ``pairId == -1``, etc.

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
        # Fetch ALL BOD data for the day via the streaming endpoint
        bod = _fetch_bod_stream(target_date)
        if bod.empty:
            logger.warning("BOD stream returned no data for %s.", target_date)
            return empty_result

        if "bmUnit" not in bod.columns or "pairId" not in bod.columns:
            logger.error("BOD data missing required columns. Columns: %s",
                         list(bod.columns))
            return empty_result

        # Filter locally to our BMUs and bids only (pairId < 0)
        bmu_set = set(bmu_ids)
        mask = (
            (bod["bmUnit"].isin(bmu_set))
            & (bod["pairId"] < 0)
        )
        bids = bod[mask].copy()

        if bids.empty:
            logger.warning(
                "No bid records found for the %d BMU IDs on %s. "
                "BOD has %d total records. Sample bmUnit values: %s",
                len(bmu_ids), target_date, len(bod),
                bod["bmUnit"].unique()[:5].tolist(),
            )
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

        logger.info("Retrieved %d bid price record(s) for %d BMU(s).",
                    len(result), result["elexonBmUnit"].nunique())
        return result

    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.error("Error in get_accepted_bid_prices: %s", exc)
        return empty_result


def calculate_daily_wind_bid_cost(
    acceptances: pd.DataFrame,
    prices: pd.DataFrame,
    target_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    Calculate the daily wind curtailment bid cost.

    This function is **decoupled** from any API logic.  It receives two
    DataFrames (accepted volumes and prices) and performs the arithmetic.

    Sign convention (GB BM cashflow):
    - ``acceptedVolumeMWh`` is **negative** (e.g. ``-12.5``) from BOAV.
    - ``price`` preserves its sign from BOD (e.g. ``-85.0`` or ``+20.0``).
    - ``cost_gbp = acceptedVolumeMWh × price``
      - ``-12.5 × -85.0 = +£1062.50`` → wind farm pays NESO (saving to system).
      - ``-12.5 × +20.0 = -£250.00``  → NESO pays wind farm (cost to system).
    - ``curtailed_mwh = abs(acceptedVolumeMWh)`` (always positive, for reporting).

    The function computes two summary blocks:
    - **All Accepted Wind Bids:** total MWh, avg £/MWh, total cost.
    - **System-Related (soFlag=True):** total MWh, avg £/MWh, total cost.
    - **Non-System (energy balancing):** the remainder.

    Parameters
    ----------
    acceptances : pd.DataFrame
        Output of :func:`get_curtailed_wind_bmus`.
        Must contain: ``elexonBmUnit``, ``settlementPeriod``, ``pairId``,
        ``acceptedVolumeMWh`` (negative), ``bmUnitName``, ``is_system_flag``.
    prices : pd.DataFrame
        Output of :func:`get_accepted_bid_prices`.
        Must contain: ``elexonBmUnit``, ``settlementPeriod``, ``pairId``,
        ``price``.
    target_date : str, optional
        Settlement date for EBOCF cross-check logging.

    Returns
    -------
    pd.DataFrame
        One row per BMU with columns:
        ``elexonBmUnit``, ``bmUnitName``, ``curtailed_mwh``,
        ``avg_price``, ``total_cost``, ``is_system_flag``.
    """
    empty_cols = ["elexonBmUnit", "bmUnitName", "curtailed_mwh",
                  "avg_price", "total_cost", "is_system_flag"]
    if acceptances.empty or prices.empty:
        logger.warning("Empty input data – returning empty result.")
        return pd.DataFrame(columns=empty_cols)

    # Ensure merge keys are consistently typed
    for df in (acceptances, prices):
        df["elexonBmUnit"] = df["elexonBmUnit"].astype(str).str.strip()
        df["settlementPeriod"] = pd.to_numeric(df["settlementPeriod"], errors="coerce").astype(int)
        df["pairId"] = pd.to_numeric(df["pairId"], errors="coerce").astype(int)

    # Ensure is_system_flag exists
    if "is_system_flag" not in acceptances.columns:
        acceptances["is_system_flag"] = False

    # Merge on the three key columns
    merged = acceptances.merge(
        prices,
        on=["elexonBmUnit", "settlementPeriod", "pairId"],
        how="inner",
    )

    if merged.empty:
        logger.warning(
            "No matching records after merge. "
            "Acceptances: %d rows, Prices: %d rows. "
            "Sample acceptance keys: %s",
            len(acceptances), len(prices),
            acceptances[["elexonBmUnit", "settlementPeriod", "pairId"]].head(3).to_dict("records"),
        )
        return pd.DataFrame(columns=empty_cols)

    # curtailed_mwh is always positive (for reporting)
    merged["curtailed_mwh"] = merged["acceptedVolumeMWh"].abs()

    # cost = volume × price (preserving signs)
    # negative volume × negative price = positive (wind farm pays NESO)
    # negative volume × positive price = negative (NESO pays wind farm)
    merged["cost_gbp"] = merged["acceptedVolumeMWh"] * merged["price"]

    # Aggregate per BMU
    summary = (
        merged
        .groupby(["elexonBmUnit", "bmUnitName", "is_system_flag"], as_index=False)
        .agg(
            curtailed_mwh=("curtailed_mwh", "sum"),
            total_cost=("cost_gbp", "sum"),
        )
    )
    summary["avg_price"] = summary["total_cost"] / summary["curtailed_mwh"].replace(0, pd.NA)
    summary["avg_price"] = summary["avg_price"].fillna(0.0)

    # Sort by total cost descending for the summary table
    summary = summary.sort_values("total_cost", ascending=False).reset_index(drop=True)

    # ------------------------------------------------------------------
    # Log both summary blocks
    # ------------------------------------------------------------------
    total_mwh_all = summary["curtailed_mwh"].sum()
    total_cost_all = summary["total_cost"].sum()
    avg_price_all = total_cost_all / total_mwh_all if total_mwh_all > 0 else 0.0

    sys_mask = summary["is_system_flag"] == True  # noqa: E712
    total_mwh_sys = summary.loc[sys_mask, "curtailed_mwh"].sum()
    total_cost_sys = summary.loc[sys_mask, "total_cost"].sum()
    avg_price_sys = total_cost_sys / total_mwh_sys if total_mwh_sys > 0 else 0.0

    total_mwh_non = total_mwh_all - total_mwh_sys
    total_cost_non = total_cost_all - total_cost_sys
    avg_price_non = total_cost_non / total_mwh_non if total_mwh_non > 0 else 0.0

    logger.info(
        "ALL WIND BIDS – %d BMU(s), %.1f MWh, avg £%.2f/MWh, total £%.2f",
        summary["elexonBmUnit"].nunique(), total_mwh_all, avg_price_all, total_cost_all,
    )
    logger.info(
        "SYSTEM (soFlag=True) – %.1f MWh, avg £%.2f/MWh, total £%.2f",
        total_mwh_sys, avg_price_sys, total_cost_sys,
    )
    logger.info(
        "NON-SYSTEM (energy) – %.1f MWh, avg £%.2f/MWh, total £%.2f",
        total_mwh_non, avg_price_non, total_cost_non,
    )

    # ------------------------------------------------------------------
    # EBOCF cross-check (optional, non-fatal)
    # ------------------------------------------------------------------
    if target_date:
        try:
            ebocf = _fetch_ebocf(
                target_date=target_date,
                bmu_ids=summary["elexonBmUnit"].tolist(),
            )
            if not ebocf.empty and "totalCashflow" in ebocf.columns:
                ebocf_total = pd.to_numeric(ebocf["totalCashflow"], errors="coerce").sum()
                logger.info(
                    "EBOCF cross-check – our total: £%.2f | EBOCF total: £%.2f | diff: £%.2f",
                    total_cost_all, ebocf_total, total_cost_all - ebocf_total,
                )
        except Exception as exc:
            logger.debug("EBOCF cross-check skipped: %s", exc)

    return summary
