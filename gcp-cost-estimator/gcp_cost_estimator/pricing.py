"""
GCP list-price constants used for estimation when real Billing Account data
isn't available.

IMPORTANT: These are public on-demand list prices captured as a snapshot and
WILL drift from reality (Google changes prices, and most clients don't pay
list price once committed-use/sustained-use discounts apply). Treat every
number this module produces as an order-of-magnitude estimate, not an
invoice. Verify against the live pricing pages before quoting a client:
  https://cloud.google.com/bigquery/pricing
  https://cloud.google.com/run/pricing
  https://cloud.google.com/appengine/pricing
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------

# Default USD -> EUR conversion rate. Override with --fx-rate on the CLI.
# We deliberately do NOT fetch a live rate to avoid a hard external
# dependency and rate-fluctuation surprises between runs; pass the current
# rate explicitly if you need precision.
DEFAULT_USD_TO_EUR = 0.92


def usd_to_eur(amount_usd: float, fx_rate: float = DEFAULT_USD_TO_EUR) -> float:
    return amount_usd * fx_rate


# ---------------------------------------------------------------------------
# BigQuery storage ($ / GiB / month), logical-bytes billing model
# ---------------------------------------------------------------------------
# Storage price is essentially flat across regions at time of writing, with a
# handful of regions priced higher. Fall back to BQ_STORAGE_DEFAULT for any
# location not listed explicitly.

BQ_STORAGE_LOGICAL_DEFAULT = {"active": 0.020, "longterm": 0.010}
BQ_STORAGE_LOGICAL_BY_LOCATION = {
    "US": {"active": 0.020, "longterm": 0.010},
    "EU": {"active": 0.020, "longterm": 0.010},
    "us-central1": {"active": 0.020, "longterm": 0.010},
    "europe-west1": {"active": 0.020, "longterm": 0.010},
}

# Physical-bytes billing model (compressed storage) - roughly 2x the logical
# rate per GiB but typically billed over far fewer GiB since it's compressed.
BQ_STORAGE_PHYSICAL_DEFAULT = {"active": 0.044, "longterm": 0.022}

BQ_FREE_ACTIVE_STORAGE_GIB = 10  # per project per month, first 10 GiB active logical storage is free

GIB = 1024 ** 3


def bq_storage_price(location: str, billing_model: str = "LOGICAL") -> dict:
    if billing_model.upper() == "PHYSICAL":
        return BQ_STORAGE_PHYSICAL_DEFAULT
    return BQ_STORAGE_LOGICAL_BY_LOCATION.get(location, BQ_STORAGE_LOGICAL_DEFAULT)


# ---------------------------------------------------------------------------
# Cloud Run (gen2, tier-1 regions e.g. us-central1/europe-west1) - on-demand
# ---------------------------------------------------------------------------

CLOUD_RUN_PRICE = {
    "cpu_per_vcpu_second": 0.000024,
    "memory_per_gib_second": 0.0000025,
    "requests_per_million": 0.40,
}

# Free tier is granted once per billing account per month, not per service -
# apply it only as a rough offset and say so in the notes.
CLOUD_RUN_FREE_TIER = {
    "vcpu_seconds": 180_000,
    "gib_seconds": 360_000,
    "requests": 2_000_000,
}

# ---------------------------------------------------------------------------
# App Engine standard - instance-hour pricing by instance class (on-demand)
# ---------------------------------------------------------------------------

APP_ENGINE_STANDARD_HOURLY = {
    "F1": 0.05,
    "F2": 0.10,
    "F4": 0.20,
    "F4_1G": 0.20,
    "B1": 0.05,
    "B2": 0.10,
    "B4": 0.20,
    "B4_1G": 0.20,
    "B8": 0.40,
}

# App Engine flex bills like a small Compute Engine VM (vCPU/mem/disk per
# hour). Approximate with generic n1-standard style on-demand rates.
APP_ENGINE_FLEX_HOURLY = {
    "vcpu_per_hour": 0.0526,
    "gib_memory_per_hour": 0.0071,
    "gib_pd_per_hour": 0.00014,
}

HOURS_PER_MONTH = 730  # average, standard estimation constant used by GCP's own calculator
SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600
