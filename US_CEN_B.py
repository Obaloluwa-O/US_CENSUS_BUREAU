import csv
import io
import logging
import os
import re
from datetime import date
from typing import Literal, Optional
from urllib.parse import quote, urlencode

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("aluminum_benchmark")

app = FastAPI(
    title="Exportwerk Aluminum Pricing Benchmark Engine",
    description=(
        "US import price benchmarks for aluminum products, "
        "built on US Census Bureau monthly trade data."
    ),
    version="1.2.1",
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
)

CENSUS_BASE_URL = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
REQUEST_TIMEOUT = 60  # seconds
MAX_RANGE_MONTHS = 36


CENSUS_API_KEY_HARDCODED = ""

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

CENSUS_VARIABLES = [
    "I_COMMODITY",
    "I_COMMODITY_SDESC",
    "GEN_VAL_MO",
    "GEN_CIF_MO",
    "GEN_QY1_MO",
    "UNIT_QY1",
    "CTY_CODE",
    "CTY_NAME",
]



def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            operation.get("responses", {}).pop("422", None)
    components = schema.get("components", {}).get("schemas", {})
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": f"{type(exc).__name__}: {exc}"},
    )



def _shift_period(period: str, months: int) -> str:
    year, month = int(period[:4]), int(period[5:7])
    idx = year * 12 + (month - 1) + months
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def _default_period() -> str:
    """Census monthly trade data lags ~6 weeks, so default to two months ago."""
    today = date.today()
    return _shift_period(f"{today.year}-{today.month:02d}", -2)


def _months_between(start: str, end: str) -> int:
    return (int(end[:4]) - int(start[:4])) * 12 + (int(end[5:7]) - int(start[5:7])) + 1


def _validate_period(value: str, name: str) -> str:
    if not PERIOD_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{name} must be in YYYY-MM format.")
    return value


def _validate_hs(hs_code: str):
    """Return (clean_code, census_level_to_fetch)."""
    hs_clean = hs_code.strip().rstrip("*")
    if not hs_clean.isdigit():
        raise HTTPException(status_code=400, detail="hs_code must contain digits only.")
    if not hs_clean.startswith("76"):
        raise HTTPException(
            status_code=400, detail="Invalid code: Aluminum commodities fall under HS Chapter 76."
        )
    if len(hs_clean) not in (2, 4, 6, 10):
        raise HTTPException(
            status_code=400, detail="hs_code must be 2, 4, 6 or 10 digits long (e.g., 76, 7601, 760110)."
        )
    return hs_clean, ("HS10" if len(hs_clean) == 10 else "HS6")


def _build_census_url(params: dict) -> str:
    """Keep the wildcard (*) and commas literal; spaces become %20."""
    query = urlencode(params, quote_via=quote, safe="*,")
    return f"{CENSUS_BASE_URL}?{query}"


def _mask_key(text: str, key: Optional[str]) -> str:
    return text.replace(key, "***") if key else text


def _fetch_census(params: dict) -> list:
    """Call the Census API and return the parsed JSON (list of rows), or [] if no data."""
    key = params.get("key")
    url = _build_census_url(params)

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.exception("Census request failed")
        raise HTTPException(
            status_code=502, detail=f"Could not reach Census API: {_mask_key(str(exc), key)}"
        )

    if response.status_code == 204:
        return []

    if response.status_code != 200:
        logger.error("Census returned HTTP %s: %s", response.status_code, response.text[:500])
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Census API returned an error",
                "census_status_code": response.status_code,
                "requested_url": _mask_key(url, key),
                "census_response": _mask_key(response.text[:500], key),
            },
        )

    try:
        return response.json()
    except ValueError:
        body = response.text[:500]
        logger.error("Census returned non-JSON (HTTP 200): %s", body)

        if "invalid key" in body.lower():
            hint = "Census says the API key is invalid. Check that you activated the key from the email Census sent you."
        elif not key:
            hint = (
                "No API key was sent. Paste it into the api_key box on this page, or into "
                "CENSUS_API_KEY_HARDCODED at the top of the file "
                "(free key: https://api.census.gov/data/key_signup.html)."
            )
        else:
            hint = "See census_response for what Census actually returned."

        raise HTTPException(
            status_code=502,
            detail={
                "message": "Census API returned a non-JSON response",
                "hint": hint,
                "content_type": response.headers.get("content-type"),
                "requested_url": _mask_key(url, key),
                "census_response": _mask_key(body, key),
            },
        )



@app.get("/api/v1/debug/raw", summary="Debug: raw Census rows")
def debug_raw(
    hs_code: str = Query("760110", description="HS code to inspect, e.g. 760110"),
    start_period: Optional[str] = Query(
        "2024-01", description="First month, YYYY-MM. Defaults to 11 months before end_period."
    ),
    end_period: Optional[str] = Query(
        "2024-12", description="Last month, YYYY-MM. Defaults to two months ago."
    ),
    output_format: Literal["json", "csv"] = Query(
        "json", alias="format", description="'csv' downloads ALL rows as a file you can open in Excel."
    ),
    api_key: Optional[str] = Query(None, description="Your Census API key (optional if set in file)."),
):
    hs_clean, level = _validate_hs(hs_code)
    end = _validate_period(end_period or _default_period(), "end_period")
    start = _validate_period(start_period or _shift_period(end, -11), "start_period")

    if start > end:
        raise HTTPException(status_code=400, detail="start_period must not be after end_period.")
    if _months_between(start, end) > MAX_RANGE_MONTHS:
        raise HTTPException(
            status_code=400, detail=f"Range too long: please request at most {MAX_RANGE_MONTHS} months."
        )

    params = {
        "get": ",".join(CENSUS_VARIABLES),
        "time": f"from {start} to {end}",
        "I_COMMODITY": f"{hs_clean}*",
        "COMM_LVL": level,
    }
    key = (api_key or CENSUS_API_KEY_HARDCODED or os.getenv("CENSUS_API_KEY") or "").strip()
    if key:
        params["key"] = key

    data = _fetch_census(params)

    if output_format == "csv":
        if len(data) < 2:
            raise HTTPException(
                status_code=404,
                detail=f"No trade records found for HS code {hs_clean} between {start} and {end}",
            )
        buffer = io.StringIO()
        csv.writer(buffer).writerows(data)  # header row + every data row
        filename = f"census_raw_{hs_clean}_{start}_to_{end}.csv"
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return {
        "start_period": start,
        "end_period": end,
        "columns": data[0] if data else [],
        "first_rows": data[1:6] if data else [],
        "row_count": max(len(data) - 1, 0),
    }