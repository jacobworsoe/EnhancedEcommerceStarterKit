"""
Find candidate GTM server-side hosting (Cloud Run or App Engine), work out
its configuration, look up its real recent usage from Cloud Monitoring, and
estimate what it costs per month - then figure out what hostname it serves.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import pricing
from .hostname_discovery import (
    app_engine_domain_mappings,
    cloud_run_domain_mapping,
    load_balancer_hostnames_for_cloud_run,
)
from .models import GTMHostingFinding

logger = logging.getLogger(__name__)

# Signals that strongly suggest a Cloud Run / App Engine service is a GTM
# server-side container, roughly in order of how the official + common
# community (e.g. stape.io) setups deploy it.
_GTM_IMAGE_SIGNALS = ["gtm-cloud-image", "cloud-tagging-10302018", "stape"]
_GTM_ENV_SIGNALS = ["CONTAINER_CONFIG", "GTM_SERVER_CONTAINER", "RUN_AS_PREVIEW_SERVER"]
_GTM_NAME_SIGNALS = ["gtm", "tag-server", "sgtm", "server-side", "stape"]


def _detect_gtm_signal(image: str, env_vars: dict, service_name: str) -> Optional[str]:
    image_lower = (image or "").lower()
    for signal in _GTM_IMAGE_SIGNALS:
        if signal in image_lower:
            return f"container image contains '{signal}'"
    for env_key in env_vars:
        if env_key in _GTM_ENV_SIGNALS:
            return f"env var '{env_key}' present"
    name_lower = service_name.lower()
    for signal in _GTM_NAME_SIGNALS:
        if signal in name_lower:
            return f"service name matches heuristic '{signal}'"
    return None


def _avg_metric_over_window(monitoring, project_id: str, metric_filter: str, days: int) -> Optional[float]:
    """Average a gauge-style metric over the trailing `days`, aligned hourly."""
    import datetime

    now = datetime.datetime.utcnow()
    interval = {
        "endTime": now.isoformat("T") + "Z",
        "startTime": (now - datetime.timedelta(days=days)).isoformat("T") + "Z",
    }
    try:
        resp = (
            monitoring.projects()
            .timeSeries()
            .list(
                name=f"projects/{project_id}",
                filter=metric_filter,
                **{
                    "interval.startTime": interval["startTime"],
                    "interval.endTime": interval["endTime"],
                    "aggregation.alignmentPeriod": "3600s",
                    "aggregation.perSeriesAligner": "ALIGN_MEAN",
                    "aggregation.crossSeriesReducer": "REDUCE_MEAN",
                    "view": "FULL",
                },
            )
            .execute()
        )
    except HttpError as exc:
        logger.debug("Monitoring query failed for %s (%s): %s", project_id, metric_filter, exc)
        return None

    points = []
    for series in resp.get("timeSeries", []):
        for point in series.get("points", []):
            value = point.get("value", {})
            points.append(value.get("doubleValue") or value.get("int64Value") or 0)

    if not points:
        return None
    return sum(float(p) for p in points) / len(points)


def _sum_metric_over_window(monitoring, project_id: str, metric_filter: str, days: int) -> Optional[float]:
    """Sum a cumulative/delta metric over the trailing `days`."""
    import datetime

    now = datetime.datetime.utcnow()
    start = now - datetime.timedelta(days=days)
    try:
        resp = (
            monitoring.projects()
            .timeSeries()
            .list(
                name=f"projects/{project_id}",
                filter=metric_filter,
                **{
                    "interval.startTime": start.isoformat("T") + "Z",
                    "interval.endTime": now.isoformat("T") + "Z",
                    "aggregation.alignmentPeriod": f"{days * 86400}s",
                    "aggregation.perSeriesAligner": "ALIGN_SUM",
                    "aggregation.crossSeriesReducer": "REDUCE_SUM",
                    "view": "FULL",
                },
            )
            .execute()
        )
    except HttpError as exc:
        logger.debug("Monitoring sum query failed for %s (%s): %s", project_id, metric_filter, exc)
        return None

    total = 0.0
    for series in resp.get("timeSeries", []):
        for point in series.get("points", []):
            value = point.get("value", {})
            total += float(value.get("doubleValue") or value.get("int64Value") or 0)
    return total


def _memory_to_gib(memory_str: str) -> float:
    if not memory_str:
        return 0.0
    memory_str = memory_str.strip()
    if memory_str.endswith("Gi"):
        return float(memory_str[:-2])
    if memory_str.endswith("Mi"):
        return float(memory_str[:-2]) / 1024
    if memory_str.endswith("M"):
        return float(memory_str[:-1]) / 1000
    if memory_str.endswith("G"):
        return float(memory_str[:-1])
    try:
        return float(memory_str) / (1024 ** 3)
    except ValueError:
        return 0.0


def discover_cloud_run_services(
    project_id: str, credentials, account_email: str, fx_rate: float
) -> List[GTMHostingFinding]:
    run_v2 = build("run", "v2", credentials=credentials, cache_discovery=False)
    run_v1 = build("run", "v1", credentials=credentials, cache_discovery=False)
    monitoring = build("monitoring", "v3", credentials=credentials, cache_discovery=False)
    compute = build("compute", "v1", credentials=credentials, cache_discovery=False)

    findings: List[GTMHostingFinding] = []
    try:
        resp = run_v2.projects().locations().services().list(
            parent=f"projects/{project_id}/locations/-"
        ).execute()
    except HttpError as exc:
        logger.debug("Cloud Run not accessible in %s: %s", project_id, exc)
        return findings

    for svc in resp.get("services", []):
        name_parts = svc["name"].split("/")  # projects/P/locations/L/services/S
        region = name_parts[3]
        service_name = name_parts[-1]

        container = (svc.get("template", {}).get("containers") or [{}])[0]
        image = container.get("image", "")
        env_vars = {e["name"]: e.get("value", "") for e in container.get("env", []) if "name" in e}
        resources = container.get("resources", {})
        limits = resources.get("limits", {})
        cpu_idle = resources.get("cpuIdle", True)

        scaling = svc.get("template", {}).get("scaling", {})
        min_instances = scaling.get("minInstanceCount", 0)
        max_instances = scaling.get("maxInstanceCount")

        signal = _detect_gtm_signal(image, env_vars, service_name)

        finding = GTMHostingFinding(
            project_id=project_id,
            account_email=account_email,
            service_type="cloud_run",
            service_name=service_name,
            region=region,
            is_likely_gtm_ss=signal is not None,
            gtm_signal=signal or "no strong signal - verify manually",
            cpu=limits.get("cpu"),
            memory=limits.get("memory"),
            min_instances=min_instances,
            max_instances=max_instances,
            cpu_always_allocated=not cpu_idle,
        )

        cpu_count = float((limits.get("cpu") or "1").rstrip("m")) / (1000 if "m" in (limits.get("cpu") or "") else 1)
        memory_gib = _memory_to_gib(limits.get("memory", "512Mi"))

        metric_filter = (
            f'metric.type="run.googleapis.com/container/instance_count" '
            f'AND resource.labels.service_name="{service_name}" '
            f'AND resource.labels.location="{region}"'
        )
        avg_7d = _avg_metric_over_window(monitoring, project_id, metric_filter, 7)
        avg_30d = _avg_metric_over_window(monitoring, project_id, metric_filter, 30)
        finding.avg_instances_7d = avg_7d
        finding.avg_instances_30d = avg_30d

        if min_instances and min_instances == max_instances:
            billable_instances = float(min_instances)
            finding.cost_confidence = "fixed-config"
        elif avg_30d is not None:
            billable_instances = avg_30d
            finding.cost_confidence = "estimated-from-usage"
        elif min_instances:
            billable_instances = float(min_instances)
            finding.cost_confidence = "fixed-config"
            finding.notes.append("No Monitoring data returned; costed off min-instances floor only.")
        else:
            billable_instances = 0.0
            finding.cost_confidence = "estimated-from-usage"
            finding.notes.append(
                "Scale-to-zero service with no Monitoring history found; cost estimate reflects "
                "only observed traffic, not a hypothetical worst case."
            )

        vcpu_seconds = billable_instances * cpu_count * pricing.SECONDS_PER_MONTH
        gib_seconds = billable_instances * memory_gib * pricing.SECONDS_PER_MONTH
        billable_vcpu_seconds = max(vcpu_seconds - pricing.CLOUD_RUN_FREE_TIER["vcpu_seconds"], 0)
        billable_gib_seconds = max(gib_seconds - pricing.CLOUD_RUN_FREE_TIER["gib_seconds"], 0)

        request_filter = (
            f'metric.type="run.googleapis.com/request_count" '
            f'AND resource.labels.service_name="{service_name}" '
            f'AND resource.labels.location="{region}"'
        )
        total_requests_30d = _sum_metric_over_window(monitoring, project_id, request_filter, 30) or 0.0
        billable_requests = max(total_requests_30d - pricing.CLOUD_RUN_FREE_TIER["requests"], 0)

        monthly_usd = (
            billable_vcpu_seconds * pricing.CLOUD_RUN_PRICE["cpu_per_vcpu_second"]
            + billable_gib_seconds * pricing.CLOUD_RUN_PRICE["memory_per_gib_second"]
            + (billable_requests / 1_000_000) * pricing.CLOUD_RUN_PRICE["requests_per_million"]
        )
        finding.monthly_cost_usd = monthly_usd
        finding.monthly_cost_eur = pricing.usd_to_eur(monthly_usd, fx_rate)

        if not cpu_idle:
            finding.notes.append("CPU always allocated - full instance-time is billed, not just per-request.")
        else:
            finding.notes.append(
                "CPU only allocated during request processing - actual cost may be lower than this "
                "estimate since idle memory-only billing isn't modeled here."
            )
        finding.notes.append("Cloud Run free tier is per billing account, not per service; only offset once here.")

        domain = cloud_run_domain_mapping(run_v1, project_id, service_name)
        if domain:
            finding.hostname = domain
            finding.hostname_source = "domain_mapping"
        else:
            lb_hosts = load_balancer_hostnames_for_cloud_run(compute, project_id, service_name)
            if lb_hosts:
                finding.hostname = ", ".join(lb_hosts)
                finding.hostname_source = "load_balancer"
            else:
                finding.hostname = svc.get("uri") or f"{service_name}-{region}.a.run.app"
                finding.hostname_source = "default_url"
                finding.notes.append(
                    "No domain mapping or load balancer route found; only the default Cloud Run URL is known. "
                    "If this is fronted by something outside GCP (e.g. Cloudflare, App proxy), verify manually."
                )

        findings.append(finding)

    return findings


def discover_app_engine_services(
    project_id: str, credentials, account_email: str, fx_rate: float
) -> List[GTMHostingFinding]:
    appengine = build("appengine", "v1", credentials=credentials, cache_discovery=False)
    monitoring = build("monitoring", "v3", credentials=credentials, cache_discovery=False)

    findings: List[GTMHostingFinding] = []
    try:
        services_resp = appengine.apps().services().list(appsId=project_id).execute()
    except HttpError as exc:
        logger.debug("App Engine not accessible in %s: %s", project_id, exc)
        return findings

    domains = app_engine_domain_mappings(appengine, project_id)

    for svc in services_resp.get("services", []):
        service_id = svc["id"]
        try:
            versions_resp = (
                appengine.apps()
                .services()
                .versions()
                .list(appsId=project_id, servicesId=service_id, view="FULL")
                .execute()
            )
        except HttpError:
            continue

        serving_versions = [
            v for v in versions_resp.get("versions", []) if v.get("servingStatus", "SERVING") == "SERVING"
        ]
        if not serving_versions:
            continue
        version = serving_versions[0]

        env = version.get("env", "standard")
        instance_class = version.get("instanceClass", "F1")
        automatic_scaling = version.get("automaticScaling", {})
        manual_scaling = version.get("manualScaling", {})
        min_instances = automatic_scaling.get("minIdleInstances") or manual_scaling.get("instances")
        max_instances = automatic_scaling.get("maxIdleInstances")

        signal = _detect_gtm_signal("", version.get("envVariables", {}) or {}, service_id)

        finding = GTMHostingFinding(
            project_id=project_id,
            account_email=account_email,
            service_type="app_engine",
            service_name=service_id,
            region="n/a (App Engine app-wide region)",
            is_likely_gtm_ss=signal is not None,
            gtm_signal=signal or "no strong signal - App Engine hosting for GTM SS is uncommon; verify manually",
            min_instances=min_instances,
            max_instances=max_instances,
        )

        metric_filter = (
            f'metric.type="appengine.googleapis.com/system/instance_count" '
            f'AND resource.labels.module_id="{service_id}"'
        )
        avg_7d = _avg_metric_over_window(monitoring, project_id, metric_filter, 7)
        avg_30d = _avg_metric_over_window(monitoring, project_id, metric_filter, 30)
        finding.avg_instances_7d = avg_7d
        finding.avg_instances_30d = avg_30d
        billable_instances = avg_30d if avg_30d is not None else float(manual_scaling.get("instances", 1))
        finding.cost_confidence = "estimated-from-usage" if avg_30d is not None else "fixed-config"

        if env == "standard":
            hourly = pricing.APP_ENGINE_STANDARD_HOURLY.get(instance_class, 0.05)
            monthly_usd = billable_instances * hourly * pricing.HOURS_PER_MONTH
        else:
            resources = version.get("resources", {})
            cpu = resources.get("cpu", 1)
            memory_gb = resources.get("memoryGb", 1)
            disk_gb = resources.get("diskGb", 10)
            rates = pricing.APP_ENGINE_FLEX_HOURLY
            hourly = (
                cpu * rates["vcpu_per_hour"]
                + memory_gb * rates["gib_memory_per_hour"]
                + disk_gb * rates["gib_pd_per_hour"]
            )
            monthly_usd = billable_instances * hourly * pricing.HOURS_PER_MONTH

        finding.monthly_cost_usd = monthly_usd
        finding.monthly_cost_eur = pricing.usd_to_eur(monthly_usd, fx_rate)
        finding.notes.append(f"App Engine {env} pricing approximated from published instance-hour rates.")

        if domains:
            finding.hostname = ", ".join(domains)
            finding.hostname_source = "domain_mapping"
        else:
            finding.hostname = f"{project_id}.appspot.com"
            finding.hostname_source = "default_url"

        findings.append(finding)

    return findings
