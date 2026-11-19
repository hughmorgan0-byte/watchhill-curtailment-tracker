"""
wasted_wind_calc.py
===================

Core calculation module for replicating wastedwind.energy Modules 1 & 2.

Module 1: Daily cost paid to curtail (switch off) wind turbines in the
GB electricity market.

Module 2: Cost of buying replacement energy (accepted offers) to replace
the curtailed wind, calculated via merit-order stack (cheapest first).

Module 2B: NIV-Adjusted replacement offer cost. For each half-hour, if the
market is long (NIV < 0), the market length absorbs part of the curtailed
wind, reducing the constraint-related offer volume required. The remaining
volume is matched against the top-of-stack (most expensive) offers for
that period.

Elexon BMRS API endpoints used
------------------------------
- ``GET /reference/bmunits/all``
    Returns a JSON array of all Balancing Mechanism Units.

- ``GET /balancing/settlement/acceptance/volumes/all/bid/{settlementDate}``
    Accepted bid volumes for a given settlement date.

- ``GET /balancing/settlement/acceptance/volumes/all/offer/{settlementDate}``
    Accepted offer volumes for a given settlement date.

- ``GET /datasets/BOD/stream?from=...&to=...``
    Bid-Offer Data (submitted bid/offer prices) – streaming endpoint.

- ``GET /datasets/BOALF/stream?from=...&to=...``
    Bid-Offer Acceptance Level Flagged – streaming endpoint.

- ``GET /balancing/settlement/system-prices/{settlementDate}``
    System prices and net imbalance volumes per settlement period.
    Response: a JSON array. Each item has ``settlementPeriod``,
    ``netImbalanceVolume``, ``systemSellPrice``, ``systemBuyPrice``, etc.

Sign convention (GB Balancing Mechanism cashflow)
-------------------------------------------------
- BOAV bid ``negative{N}`` volume is **negative** MWh.
- BOAV offer ``positive{N}`` volume is **positive** MWh.
- BOD ``bid``/``offer`` price preserves its sign.
- Bid cashflow: ``cost = volume × price`` (volume negative).
- Offer cashflow: ``cost = volume × price`` (volume positive).
- NIV: negative = market long (surplus), positive/zero = market short.
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
API_CALL_DELAY = 1.0

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BOD stream cache (avoid fetching the same day's data twice)
# ---------------------------------------------------------------------------
_bod_cache: dict[str, pd.DataFrame] = {}


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
    """
    url = f"{API_BASE}/reference/bmunits/all"
    logger.info("Fetching BMU reference from %s", url)
    resp = _get_with_retry(url)
    data = resp.json()

    if isinstance(data, dict):
        for key in ("data", "bmunits", "results"):
            if key in data:
                data = data[key]
                break

    df = pd.DataFrame(data)
    if df.empty:
        return df

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()

    return df


def _fetch_boav_bid(target_date: str, bmu_ids: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Fetch accepted bid volumes (BOAV) for a given settlement date.

    Endpoint: ``GET {API_BASE}/balancing/settlement/acceptance/volumes/all/bid/{settlementDate}``
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

    if isinstance(payload, dict):
        records = payload.get("data", [])
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    if not records:
        logger.warning("BOAV bid returned no data for %s.", target_date)
        return pd.DataFrame()

    flat_records: list[dict] = []
    for rec in records:
        pair_vols = rec.pop("pairVolumes", None) or {}
        for n in range(1, 7):
            rec[f"negative{n}"] = pair_vols.get(f"negative{n}")
            rec[f"positive{n}"] = pair_vols.get(f"positive{n}")
        flat_records.append(rec)

    df = pd.DataFrame(flat_records)

    numeric_cols = (
        ["settlementPeriod", "acceptanceId", "totalVolumeAccepted"]
        + [f"negative{n}" for n in range(1, 7)]
        + [f"positive{n}" for n in range(1, 7)]
    )
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate",
                "bmUnitType", "leadPartyName", "acceptanceDuration"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    return df


def _fetch_boav_offer(target_date: str, bmu_ids: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Fetch accepted offer volumes (BOAV) for a given settlement date.

    Endpoint: ``GET {API_BASE}/balancing/settlement/acceptance/volumes/all/offer/{settlementDate}``
    """
    url = f"{API_BASE}/balancing/settlement/acceptance/volumes/all/offer/{target_date}"
    params: dict = {}
    if bmu_ids:
        params["bmUnit"] = bmu_ids

    logger.info("Fetching BOAV offer for %s (%d BMU filter%s)",
                target_date, len(bmu_ids) if bmu_ids else 0,
                "" if bmu_ids else "s – all")
    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    if isinstance(payload, dict):
        records = payload.get("data", [])
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    if not records:
        logger.warning("BOAV offer returned no data for %s.", target_date)
        return pd.DataFrame()

    flat_records: list[dict] = []
    for rec in records:
        pair_vols = rec.pop("pairVolumes", None) or {}
        for n in range(1, 7):
            rec[f"negative{n}"] = pair_vols.get(f"negative{n}")
            rec[f"positive{n}"] = pair_vols.get(f"positive{n}")
        flat_records.append(rec)

    df = pd.DataFrame(flat_records)

    numeric_cols = (
        ["settlementPeriod", "acceptanceId", "totalVolumeAccepted"]
        + [f"negative{n}" for n in range(1, 7)]
        + [f"positive{n}" for n in range(1, 7)]
    )
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate",
                "bmUnitType", "leadPartyName", "acceptanceDuration"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    return df


def _fetch_bod_stream(target_date: str) -> pd.DataFrame:
    """
    Fetch Bid-Offer Data (BOD) for a given settlement date using the
    streaming endpoint. Results are cached per date to avoid duplicate fetches.

    Endpoint: ``GET {API_BASE}/datasets/BOD/stream``
    """
    if target_date in _bod_cache:
        logger.info("BOD stream for %s served from cache.", target_date)
        return _bod_cache[target_date].copy()

    url = f"{API_BASE}/datasets/BOD/stream"

    dt = datetime.strptime(target_date, "%Y-%m-%d")
    next_dt = dt + timedelta(days=1)
    from_str = dt.strftime("%Y-%m-%dT00:00Z")
    to_str = next_dt.strftime("%Y-%m-%dT00:00Z")

    params: dict = {
        "from": from_str,
        "to": to_str,
    }

    logger.info("Fetching BOD stream for %s (full day, filtering locally)", target_date)
    resp = _get_with_retry(url, params=params)
    payload = resp.json()

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("data", [])
    else:
        records = []

    if not records:
        logger.warning("BOD stream returned no data for %s.", target_date)
        return pd.DataFrame()

    df = pd.DataFrame(records)

    for col in ("settlementPeriod", "levelFrom", "levelTo", "pairId", "bid", "offer"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    logger.info("BOD stream returned %d total record(s) for %s.", len(df), target_date)

    _bod_cache[target_date] = df.copy()
    return df


def _fetch_boalf_stream(target_date: str) -> pd.DataFrame:
    """
    Fetch Bid-Offer Acceptance Level Flagged (BOALF) data for a given
    settlement date using the streaming endpoint.

    Endpoint: ``GET {API_BASE}/datasets/BOALF/stream``
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

    for col in ("settlementPeriodFrom", "settlementPeriodTo",
                "levelFrom", "levelTo", "acceptanceNumber"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ("soFlag", "deemedBoFlag", "storFlag", "rrFlag"):
        if col in df.columns:
            df[col] = df[col].astype(bool)

    for col in ("bmUnit", "nationalGridBmUnit", "settlementDate"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    logger.info("BOALF stream returned %d record(s) for %s.", len(df), target_date)
    return df


def _fetch_ebocf(target_date: str, bmu_ids: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Fetch Elexon's official Indicative Period BMU Cashflows (EBOCF) for cross-checking.
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

        if bmu_ids and "bmUnit" in df.columns:
            df = df[df["bmUnit"].isin(bmu_ids)].reset_index(drop=True)

        logger.info("EBOCF returned %d record(s) for cross-check.", len(df))
        return df

    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("Could not fetch EBOCF for cross-check: %s", exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# MODULE 1 – Wind Curtailment Bid Cost
# ---------------------------------------------------------------------------
def get_curtailed_wind_bmus(target_date: str) -> pd.DataFrame:
    """
    Identify all Wind BMUs that had an **accepted bid** (curtailed) on
    ``target_date``.

    Returns
    -------
    pd.DataFrame
        Columns: ``elexonBmUnit``, ``bmUnitName``, ``settlementPeriod``,
        ``pairId``, ``acceptedVolumeMWh``, ``is_system_flag``.
    """
    empty_result = pd.DataFrame(columns=[
        "elexonBmUnit", "bmUnitName", "settlementPeriod",
        "pairId", "acceptedVolumeMWh", "is_system_flag",
    ])

    try:
        bmu_ref = _fetch_bmu_reference()
        if bmu_ref.empty:
            logger.error("BMU reference returned no data.")
            return empty_result

        if "fuelType" not in bmu_ref.columns:
            logger.error("BMU reference missing 'fuelType' column.")
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

        name_map: dict[str, str] = {}
        if "bmUnitName" in wind_bmus.columns:
            for _, row in wind_bmus.iterrows():
                name_map[row["elexonBmUnit"]] = row["bmUnitName"]

        time.sleep(API_CALL_DELAY)
        boav = _fetch_boav_bid(target_date)
        if boav.empty:
            logger.warning("BOAV bid returned no data for %s.", target_date)
            return empty_result

        if "bmUnit" not in boav.columns:
            logger.error("BOAV data missing 'bmUnit' column.")
            return empty_result

        wind_boav = boav[boav["bmUnit"].isin(wind_bmu_ids)].copy()
        if wind_boav.empty:
            logger.info("No BOAV bid records for wind BMUs on %s.", target_date)
            return empty_result

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
                        "acceptedVolumeMWh": float(vol),
                        "acceptanceId": acceptance_id,
                    })

        if not rows:
            logger.info("No accepted bid volumes found for wind BMUs on %s.", target_date)
            return empty_result

        result = pd.DataFrame(rows)

        time.sleep(API_CALL_DELAY)
        boalf = _fetch_boalf_stream(target_date)

        if not boalf.empty and "acceptanceNumber" in boalf.columns and "soFlag" in boalf.columns:
            system_flags = set()
            for _, boalf_row in boalf.iterrows():
                if boalf_row.get("soFlag") is True:
                    bmu_key = str(boalf_row.get("bmUnit", "")).strip()
                    acc_num = boalf_row.get("acceptanceNumber")
                    if bmu_key and acc_num is not None and not pd.isna(acc_num):
                        system_flags.add((bmu_key, int(acc_num)))

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
            result["is_system_flag"] = False
            logger.warning(
                "BOALF data unavailable. Defaulting all to is_system_flag=False."
            )

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
    Uses the cached BOD stream endpoint.
    """
    empty_result = pd.DataFrame(columns=["elexonBmUnit", "settlementPeriod", "pairId", "price"])

    try:
        time.sleep(API_CALL_DELAY)
        bod = _fetch_bod_stream(target_date)
        if bod.empty:
            logger.warning("BOD stream returned no data for %s.", target_date)
            return empty_result

        if "bmUnit" not in bod.columns or "pairId" not in bod.columns:
            logger.error("BOD data missing required columns.")
            return empty_result

        bmu_set = set(bmu_ids)
        mask = (
            (bod["bmUnit"].isin(bmu_set))
            & (bod["pairId"] < 0)
        )
        bids = bod[mask].copy()

        if bids.empty:
            logger.warning("No bid records found for the %d BMU IDs on %s.",
                           len(bmu_ids), target_date)
            return empty_result

        if "bid" not in bids.columns:
            logger.error("BOD data missing 'bid' price column.")
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
    Calculate the daily wind curtailment bid cost (Module 1).
    """
    empty_cols = ["elexonBmUnit", "bmUnitName", "curtailed_mwh",
                  "avg_price", "total_cost", "is_system_flag"]
    if acceptances.empty or prices.empty:
        logger.warning("Empty input data – returning empty result.")
        return pd.DataFrame(columns=empty_cols)

    for df in (acceptances, prices):
        df["elexonBmUnit"] = df["elexonBmUnit"].astype(str).str.strip()
        df["settlementPeriod"] = pd.to_numeric(df["settlementPeriod"], errors="coerce").astype(int)
        df["pairId"] = pd.to_numeric(df["pairId"], errors="coerce").astype(int)

    if "is_system_flag" not in acceptances.columns:
        acceptances["is_system_flag"] = False

    merged = acceptances.merge(
        prices,
        on=["elexonBmUnit", "settlementPeriod", "pairId"],
        how="inner",
    )

    if merged.empty:
        logger.warning("No matching records after merge.")
        return pd.DataFrame(columns=empty_cols)

    merged["curtailed_mwh"] = merged["acceptedVolumeMWh"].abs()
    merged["cost_gbp"] = merged["acceptedVolumeMWh"] * merged["price"]

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
    summary = summary.sort_values("total_cost", ascending=False).reset_index(drop=True)

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

    if target_date:
        try:
            ebocf = _fetch_ebocf(target_date=target_date,
                                bmu_ids=summary["elexonBmUnit"].tolist())
            if not ebocf.empty and "totalCashflow" in ebocf.columns:
                ebocf_total = pd.to_numeric(ebocf["totalCashflow"], errors="coerce").sum()
                logger.info(
                    "EBOCF cross-check – our total: £%.2f | EBOCF total: £%.2f | diff: £%.2f",
                    total_cost_all, ebocf_total, total_cost_all - ebocf_total,
                )
        except Exception as exc:
            logger.debug("EBOCF cross-check skipped: %s", exc)

    return summary


# ---------------------------------------------------------------------------
# MODULE 2 – Replacement Energy (Accepted Offer) Merit-Order Cost
# ---------------------------------------------------------------------------
def get_accepted_offer_volumes(target_date: str) -> pd.DataFrame:
    """
    Function 4: Retrieve all accepted offer volumes across ALL BMUs for
    ``target_date``.

    Queries ``/balancing/settlement/acceptance/volumes/all/offer/{target_date}``
    and extracts non-zero ``positive1`` through ``positive6`` values.
    Joins against the BMU reference for ``fuelType`` and ``bmUnitName``.
    """
    empty_result = pd.DataFrame(columns=[
        "bmUnit", "assetName", "fuelType",
        "settlementPeriod", "pairId", "offer_mwh",
    ])

    try:
        bmu_ref = _fetch_bmu_reference()
        name_map: dict[str, str] = {}
        fuel_map: dict[str, str] = {}
        if not bmu_ref.empty and "elexonBmUnit" in bmu_ref.columns:
            for _, row in bmu_ref.iterrows():
                bmu_id = row["elexonBmUnit"]
                name_map[bmu_id] = row.get("bmUnitName", "Unknown")
                fuel_map[bmu_id] = row.get("fuelType", "UNKNOWN")

        time.sleep(API_CALL_DELAY)
        boav = _fetch_boav_offer(target_date)
        if boav.empty:
            logger.warning("BOAV offer returned no data for %s.", target_date)
            return empty_result

        if "bmUnit" not in boav.columns:
            logger.error("BOAV offer data missing 'bmUnit' column.")
            return empty_result

        rows: list[dict] = []
        for _, rec in boav.iterrows():
            bmu = rec["bmUnit"]
            period = int(rec["settlementPeriod"])
            name = name_map.get(bmu, "Unknown")
            fuel = fuel_map.get(bmu, "UNKNOWN")

            for n in range(1, 7):
                vol = rec.get(f"positive{n}")
                if vol is not None and not pd.isna(vol) and vol != 0:
                    rows.append({
                        "bmUnit": bmu,
                        "assetName": name,
                        "fuelType": fuel,
                        "settlementPeriod": period,
                        "pairId": n,
                        "offer_mwh": float(vol),
                    })

        if not rows:
            logger.info("No accepted offer volumes found on %s.", target_date)
            return empty_result

        result = pd.DataFrame(rows)
        logger.info(
            "Identified %d accepted offer record(s) across %d BMU(s).",
            len(result), result["bmUnit"].nunique(),
        )
        return result

    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.error("Error in get_accepted_offer_volumes: %s", exc)
        return empty_result


def get_accepted_offer_prices(target_date: str, offer_bmu_ids: list[str]) -> pd.DataFrame:
    """
    Function 5: Retrieve the offer prices (£/MWh) for the accepted offers.

    Reuses the cached BOD stream fetch (shared with Module 1).
    Filters to ``pairId > 0`` (offers) for the given BMU IDs.
    """
    empty_result = pd.DataFrame(columns=["bmUnit", "settlementPeriod", "pairId", "price"])

    try:
        bod = _fetch_bod_stream(target_date)
        if bod.empty:
            logger.warning("BOD stream returned no data for %s.", target_date)
            return empty_result

        if "bmUnit" not in bod.columns or "pairId" not in bod.columns:
            logger.error("BOD data missing required columns.")
            return empty_result

        bmu_set = set(offer_bmu_ids)
        mask = (
            (bod["bmUnit"].isin(bmu_set))
            & (bod["pairId"] > 0)
        )
        offers = bod[mask].copy()

        if offers.empty:
            logger.warning("No offer records found for the %d BMU IDs on %s.",
                           len(offer_bmu_ids), target_date)
            return empty_result

        if "offer" not in offers.columns:
            logger.error("BOD data missing 'offer' price column.")
            return empty_result

        result = pd.DataFrame({
            "bmUnit": offers["bmUnit"].values,
            "settlementPeriod": offers["settlementPeriod"].astype(int).values,
            "pairId": offers["pairId"].astype(int).values,
            "price": offers["offer"].values,
        })

        logger.info("Retrieved %d offer price record(s) for %d BMU(s).",
                    len(result), result["bmUnit"].nunique())
        return result

    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.error("Error in get_accepted_offer_prices: %s", exc)
        return empty_result


def calculate_replacement_offer_cost(
    bid_results: pd.DataFrame,
    offer_stack: pd.DataFrame,
) -> dict:
    """
    Function 6: Merit-order replacement cost calculation (Module 2).

    Takes the total curtailed wind volume from Module 1 and walks down
    the sorted offer stack (cheapest first) to determine the cost of
    buying replacement energy.
    """
    target_mwh = float(bid_results["curtailed_mwh"].sum()) if not bid_results.empty else 0.0

    if offer_stack.empty or target_mwh <= 0:
        logger.warning("No offer data or zero target – returning empty result.")
        return {
            "target_mwh": target_mwh,
            "matched_mwh": 0.0,
            "replacement_cost_gbp": 0.0,
            "weighted_avg_price": 0.0,
            "fuel_breakdown": pd.DataFrame(columns=["fuelType", "mwh", "cost_gbp", "avg_price"]),
            "top_bmus": pd.DataFrame(columns=["bmUnit", "assetName", "fuelType", "mwh", "cost_gbp"]),
            "total_offers_available": 0.0,
            "shortfall": True,
        }

    offer_stack = offer_stack.copy()
    offer_stack["slice_cost"] = offer_stack["offer_mwh"] * offer_stack["price"]

    total_offers_available = float(offer_stack["offer_mwh"].sum())
    shortfall = total_offers_available < target_mwh

    if shortfall:
        logger.warning(
            "Total accepted offers (%.1f MWh) < curtailed wind (%.1f MWh). "
            "Using all available offers.",
            total_offers_available, target_mwh,
        )

    offer_stack = offer_stack.sort_values("price", ascending=True).reset_index(drop=True)

    cumulative_mwh = 0.0
    cumulative_cost = 0.0
    matched_rows: list[dict] = []

    for _, row in offer_stack.iterrows():
        if cumulative_mwh >= target_mwh:
            break

        remaining = target_mwh - cumulative_mwh
        allocated = min(row["offer_mwh"], remaining)

        if row["offer_mwh"] > 0:
            allocated_cost = allocated * row["price"]
        else:
            allocated_cost = 0.0

        cumulative_mwh += allocated
        cumulative_cost += allocated_cost

        matched_rows.append({
            "bmUnit": row["bmUnit"],
            "assetName": row.get("assetName", "Unknown"),
            "fuelType": row.get("fuelType", "UNKNOWN"),
            "settlementPeriod": row["settlementPeriod"],
            "pairId": row["pairId"],
            "offer_mwh": row["offer_mwh"],
            "allocated_mwh": allocated,
            "price": row["price"],
            "allocated_cost": allocated_cost,
        })

    matched_df = pd.DataFrame(matched_rows) if matched_rows else pd.DataFrame()

    matched_mwh = float(matched_df["allocated_mwh"].sum()) if not matched_df.empty else 0.0
    replacement_cost = float(matched_df["allocated_cost"].sum()) if not matched_df.empty else 0.0
    weighted_avg = replacement_cost / matched_mwh if matched_mwh > 0 else 0.0

    fuel_breakdown = pd.DataFrame(columns=["fuelType", "mwh", "cost_gbp", "avg_price"])
    if not matched_df.empty:
        fuel_breakdown = (
            matched_df
            .groupby("fuelType", as_index=False)
            .agg(
                mwh=("allocated_mwh", "sum"),
                cost_gbp=("allocated_cost", "sum"),
            )
        )
        fuel_breakdown["avg_price"] = (
            fuel_breakdown["cost_gbp"] / fuel_breakdown["mwh"].replace(0, pd.NA)
        ).fillna(0.0)
        fuel_breakdown = fuel_breakdown.sort_values("cost_gbp", ascending=False).reset_index(drop=True)

    top_bmus = pd.DataFrame(columns=["bmUnit", "assetName", "fuelType", "mwh", "cost_gbp"])
    if not matched_df.empty:
        top_bmus = (
            matched_df
            .groupby(["bmUnit", "assetName", "fuelType"], as_index=False)
            .agg(
                mwh=("allocated_mwh", "sum"),
                cost_gbp=("allocated_cost", "sum"),
            )
            .sort_values("cost_gbp", ascending=False)
            .head(10)
            .reset_index(drop=True)
        )

    logger.info(
        "MODULE 2 – Target: %.1f MWh | Matched: %.1f MWh | "
        "Cost: £%.2f | Avg: £%.2f/MWh | Shortfall: %s",
        target_mwh, matched_mwh, replacement_cost, weighted_avg, shortfall,
    )

    return {
        "target_mwh": target_mwh,
        "matched_mwh": matched_mwh,
        "replacement_cost_gbp": replacement_cost,
        "weighted_avg_price": weighted_avg,
        "fuel_breakdown": fuel_breakdown,
        "top_bmus": top_bmus,
        "total_offers_available": total_offers_available,
        "shortfall": shortfall,
    }


# ---------------------------------------------------------------------------
# MODULE 2B – NIV-Adjusted Replacement Offer Cost (Half-Hourly, Top-of-Stack)
# ---------------------------------------------------------------------------
def get_net_imbalance_volumes(target_date: str) -> dict[int, float]:
    """
    Function 7: Retrieve Net Imbalance Volume (NIV) per settlement period.

    Queries ``GET {API_BASE}/balancing/settlement/system-prices/{target_date}``
    and extracts ``settlementPeriod`` and ``netImbalanceVolume`` for each
    half-hour. If multiple rows exist for the same period (e.g. multiple
    runs), keeps the latest by ``createdDateTime``.

    Sign convention:
    - ``netImbalanceVolume < 0`` → market is LONG (surplus).
    - ``netImbalanceVolume >= 0`` → market is SHORT or balanced.

    Parameters
    ----------
    target_date : str
        Settlement date ``YYYY-MM-DD``.

    Returns
    -------
    dict[int, float]
        Mapping of ``settlementPeriod`` (1-48) to ``netImbalanceVolume`` (MWh).
        Empty dict if no data available.
    """
    url = f"{API_BASE}/balancing/settlement/system-prices/{target_date}"
    logger.info("Fetching system prices / NIV for %s", target_date)

    try:
        resp = _get_with_retry(url)
        payload = resp.json()

        if isinstance(payload, dict):
            records = payload.get("data", [])
        elif isinstance(payload, list):
            records = payload
        else:
            records = []

        if not records:
            logger.warning("System prices returned no data for %s.", target_date)
            return {}

        # Deduplicate by settlementPeriod, keeping the latest createdDateTime
        best: dict[int, tuple[str, float]] = {}
        for rec in records:
            sp = rec.get("settlementPeriod")
            niv = rec.get("netImbalanceVolume")
            created = rec.get("createdDateTime", "")
            if sp is None or niv is None:
                continue
            sp = int(sp)
            niv = float(niv)
            if sp not in best or created > best[sp][0]:
                best[sp] = (created, niv)

        result = {sp: niv for sp, (_, niv) in best.items()}
        logger.info(
            "NIV: %d periods | long periods: %d | short/balanced: %d",
            len(result),
            sum(1 for v in result.values() if v < 0),
            sum(1 for v in result.values() if v >= 0),
        )
        return result

    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("Could not fetch NIV data: %s", exc)
        return {}


def calculate_niv_adjusted_offer_cost(
    bid_records: pd.DataFrame,
    offer_stack: pd.DataFrame,
    niv_by_period: dict[int, float],
) -> dict:
    """
    Function 8: NIV-Adjusted half-hourly replacement offer cost (Module 2B).

    For each settlement period:
    - If NIV < 0 (market long): the market length absorbs part of the
      curtailed wind, reducing the constraint-related offer volume needed.
      ``required = max(0, wind_curtailed - abs(niv))``
    - If NIV >= 0 (market short/balanced): all curtailed wind needs
      replacement. ``required = wind_curtailed``
    - Sort that period's offer slices DESCENDING by price (most expensive
      first – top-of-stack attribution to constraint).
    - Walk down until cumulative MWh reaches the required volume.

    Parameters
    ----------
    bid_records : pd.DataFrame
        Module 1 acceptances with columns: ``elexonBmUnit``,
        ``settlementPeriod``, ``acceptedVolumeMWh`` (negative).
    offer_stack : pd.DataFrame
        Merged offer volumes + prices with columns: ``bmUnit``,
        ``assetName``, ``fuelType``, ``settlementPeriod``, ``pairId``,
        ``offer_mwh``, ``price``.
    niv_by_period : dict[int, float]
        Mapping of settlement period to NIV (MWh). Negative = long.

    Returns
    -------
    dict
        Keys:
        - ``total_wind_curtailed_mwh``
        - ``total_long_market_offset_mwh``
        - ``total_niv_adjusted_offer_mwh``
        - ``matched_offer_mwh``
        - ``unmatched_shortfall_mwh``
        - ``niv_adjusted_offer_cost_gbp``
        - ``weighted_avg_offer_price``
        - ``fuel_breakdown``: DataFrame
        - ``period_detail``: DataFrame (per-period breakdown)
    """
    empty_result = {
        "total_wind_curtailed_mwh": 0.0,
        "total_long_market_offset_mwh": 0.0,
        "total_niv_adjusted_offer_mwh": 0.0,
        "matched_offer_mwh": 0.0,
        "unmatched_shortfall_mwh": 0.0,
        "niv_adjusted_offer_cost_gbp": 0.0,
        "weighted_avg_offer_price": 0.0,
        "fuel_breakdown": pd.DataFrame(columns=["fuelType", "mwh", "cost_gbp", "avg_price"]),
        "period_detail": pd.DataFrame(columns=[
            "settlementPeriod", "wind_mwh", "niv", "long_offset",
            "required_mwh", "matched_mwh", "shortfall_mwh", "cost_gbp",
        ]),
    }

    if bid_records.empty or offer_stack.empty:
        logger.warning("NIV-adjusted: empty input data.")
        return empty_result

    # Group wind curtailed MWh by settlement period (absolute value)
    bid_records = bid_records.copy()
    bid_records["curtailed_mwh"] = bid_records["acceptedVolumeMWh"].abs()
    wind_by_period = bid_records.groupby("settlementPeriod")["curtailed_mwh"].sum().to_dict()

    # Deduplicate offer stack on (bmUnit, settlementPeriod, pairId) keeping first
    offer_stack = offer_stack.copy()
    offer_stack = offer_stack.drop_duplicates(
        subset=["bmUnit", "settlementPeriod", "pairId"], keep="first"
    ).reset_index(drop=True)

    # Group offers by settlement period
    offers_by_period: dict[int, pd.DataFrame] = {}
    for sp, group in offer_stack.groupby("settlementPeriod"):
        offers_by_period[int(sp)] = group

    # Walk through each settlement period
    total_wind = 0.0
    total_long_offset = 0.0
    total_required = 0.0
    total_matched = 0.0
    total_shortfall = 0.0
    total_cost = 0.0
    period_rows: list[dict] = []
    all_matched_slices: list[dict] = []

    for sp in range(1, SETTLEMENT_PERIODS + 1):
        wind_mwh = wind_by_period.get(sp, 0.0)
        if wind_mwh <= 0:
            continue

        total_wind += wind_mwh
        niv = niv_by_period.get(sp, 0.0)

        if niv < 0:
            long_offset = min(abs(niv), wind_mwh)
            required = max(0.0, wind_mwh - abs(niv))
        else:
            long_offset = 0.0
            required = wind_mwh

        total_long_offset += long_offset
        total_required += required

        if required <= 0:
            period_rows.append({
                "settlementPeriod": sp,
                "wind_mwh": wind_mwh,
                "niv": niv,
                "long_offset": long_offset,
                "required_mwh": 0.0,
                "matched_mwh": 0.0,
                "shortfall_mwh": 0.0,
                "cost_gbp": 0.0,
            })
            continue

        # Get offers for this period, sort DESCENDING by price (top-of-stack)
        period_offers = offers_by_period.get(sp, pd.DataFrame())
        if period_offers.empty:
            total_shortfall += required
            period_rows.append({
                "settlementPeriod": sp,
                "wind_mwh": wind_mwh,
                "niv": niv,
                "long_offset": long_offset,
                "required_mwh": required,
                "matched_mwh": 0.0,
                "shortfall_mwh": required,
                "cost_gbp": 0.0,
            })
            continue

        period_offers = period_offers.sort_values("price", ascending=False).reset_index(drop=True)

        # Walk down the top-of-stack
        cumulative = 0.0
        period_cost = 0.0
        period_matched = 0.0

        for _, row in period_offers.iterrows():
            if cumulative >= required:
                break

            remaining = required - cumulative
            allocated = min(row["offer_mwh"], remaining)
            allocated_cost = allocated * row["price"]

            cumulative += allocated
            period_cost += allocated_cost
            period_matched += allocated

            all_matched_slices.append({
                "bmUnit": row["bmUnit"],
                "assetName": row.get("assetName", "Unknown"),
                "fuelType": row.get("fuelType", "UNKNOWN"),
                "settlementPeriod": sp,
                "pairId": row["pairId"],
                "allocated_mwh": allocated,
                "price": row["price"],
                "allocated_cost": allocated_cost,
            })

        period_shortfall = max(0.0, required - cumulative)
        total_matched += period_matched
        total_shortfall += period_shortfall
        total_cost += period_cost

        period_rows.append({
            "settlementPeriod": sp,
            "wind_mwh": wind_mwh,
            "niv": niv,
            "long_offset": long_offset,
            "required_mwh": required,
            "matched_mwh": period_matched,
            "shortfall_mwh": period_shortfall,
            "cost_gbp": period_cost,
        })

    # Build fuel breakdown
    fuel_breakdown = pd.DataFrame(columns=["fuelType", "mwh", "cost_gbp", "avg_price"])
    if all_matched_slices:
        slices_df = pd.DataFrame(all_matched_slices)
        fuel_breakdown = (
            slices_df
            .groupby("fuelType", as_index=False)
            .agg(
                mwh=("allocated_mwh", "sum"),
                cost_gbp=("allocated_cost", "sum"),
            )
        )
        fuel_breakdown["avg_price"] = (
            fuel_breakdown["cost_gbp"] / fuel_breakdown["mwh"].replace(0, pd.NA)
        ).fillna(0.0)
        fuel_breakdown = fuel_breakdown.sort_values("cost_gbp", ascending=False).reset_index(drop=True)

    period_detail = pd.DataFrame(period_rows) if period_rows else pd.DataFrame(columns=[
        "settlementPeriod", "wind_mwh", "niv", "long_offset",
        "required_mwh", "matched_mwh", "shortfall_mwh", "cost_gbp",
    ])

    weighted_avg = total_cost / total_matched if total_matched > 0 else 0.0

    logger.info(
        "MODULE 2B – Wind: %.1f MWh | Long offset: %.1f MWh | "
        "Required: %.1f MWh | Matched: %.1f MWh | Shortfall: %.1f MWh | "
        "Cost: £%.2f | Avg: £%.2f/MWh",
        total_wind, total_long_offset, total_required,
        total_matched, total_shortfall, total_cost, weighted_avg,
    )

    return {
        "total_wind_curtailed_mwh": total_wind,
        "total_long_market_offset_mwh": total_long_offset,
        "total_niv_adjusted_offer_mwh": total_required,
        "matched_offer_mwh": total_matched,
        "unmatched_shortfall_mwh": total_shortfall,
        "niv_adjusted_offer_cost_gbp": total_cost,
        "weighted_avg_offer_price": weighted_avg,
        "fuel_breakdown": fuel_breakdown,
        "period_detail": period_detail,
    }
