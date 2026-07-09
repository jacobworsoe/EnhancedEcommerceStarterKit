"""Data structures shared across the estimator modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    """A Google account we hold an OAuth credential for."""

    email: str
    credentials: object  # google.oauth2.credentials.Credentials


@dataclass
class ProjectInfo:
    project_id: str
    project_number: str
    display_name: str
    account_email: str
    billing_account_name: Optional[str] = None  # e.g. "billingAccounts/012345-ABCDEF-678901"
    billing_enabled: bool = False
    # Every other account (besides account_email) that also has visibility
    # into this same project. Populated when discovery finds the same
    # project_id reachable through more than one authorized account.
    also_visible_via: list = field(default_factory=list)


@dataclass
class GA4ExportFinding:
    project_id: str
    account_email: str
    dataset_id: str
    location: str
    property_id: Optional[str] = None
    storage_billing_model: str = "LOGICAL"  # LOGICAL or PHYSICAL
    table_count: int = 0
    oldest_table_date: Optional[str] = None
    newest_table_date: Optional[str] = None
    active_bytes: int = 0
    longterm_bytes: int = 0
    monthly_cost_usd: float = 0.0
    monthly_cost_eur: float = 0.0
    cost_source: str = "estimated"  # "billing_export" or "estimated"
    also_visible_via: list = field(default_factory=list)
    notes: list = field(default_factory=list)


@dataclass
class GTMHostingFinding:
    project_id: str
    account_email: str
    service_type: str  # "cloud_run" or "app_engine"
    service_name: str
    region: str
    is_likely_gtm_ss: bool
    gtm_signal: str  # what tipped the heuristic (image name, env var, etc.)
    cpu: Optional[str] = None
    memory: Optional[str] = None
    min_instances: Optional[int] = None
    max_instances: Optional[int] = None
    cpu_always_allocated: Optional[bool] = None
    avg_instances_7d: Optional[float] = None
    avg_instances_30d: Optional[float] = None
    monthly_cost_usd: float = 0.0
    monthly_cost_eur: float = 0.0
    # "fixed-config" | "estimated-from-usage" | "billing_export" | "billing_export-prorated"
    cost_confidence: str = "estimated"
    hostname: Optional[str] = None
    hostname_source: str = "none"  # "domain_mapping" | "load_balancer" | "default_url" | "none"
    also_visible_via: list = field(default_factory=list)
    notes: list = field(default_factory=list)
