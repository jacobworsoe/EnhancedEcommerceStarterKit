"""Console summary + CSV/JSON export of findings."""

from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path
from typing import List

from .models import GA4ExportFinding, GTMHostingFinding


def _fmt_eur(amount: float) -> str:
    return f"EUR {amount:,.2f}"


def print_console_report(ga4_findings: List[GA4ExportFinding], gtm_findings: List[GTMHostingFinding]) -> None:
    print("\n" + "=" * 100)
    print("GA4 -> BigQuery EXPORT STORAGE COST")
    print("=" * 100)
    if not ga4_findings:
        print("No GA4 BigQuery export datasets found.")
    for f in ga4_findings:
        gib_active = f.active_bytes / (1024 ** 3)
        gib_longterm = f.longterm_bytes / (1024 ** 3)
        print(
            f"\n[{f.project_id}] dataset={f.dataset_id} (property {f.property_id or '?'}) "
            f"account={f.account_email}"
        )
        print(
            f"  {f.table_count} daily tables, {f.oldest_table_date or '?'} -> {f.newest_table_date or '?'} "
            f"| location={f.location} billing_model={f.storage_billing_model}"
        )
        print(f"  active={gib_active:,.2f} GiB  long-term={gib_longterm:,.2f} GiB")
        print(f"  Estimated monthly cost: {_fmt_eur(f.monthly_cost_eur)}  (source: {f.cost_source})")
        for note in f.notes:
            print(f"  note: {note}")

    print("\n" + "=" * 100)
    print("GTM SERVER-SIDE HOSTING CANDIDATES (Cloud Run / App Engine)")
    print("=" * 100)
    if not gtm_findings:
        print("No Cloud Run or App Engine services found.")
    for f in gtm_findings:
        flag = "LIKELY GTM SS" if f.is_likely_gtm_ss else "unconfirmed"
        print(
            f"\n[{f.project_id}] {f.service_type} '{f.service_name}' ({f.region}) "
            f"account={f.account_email} - {flag} ({f.gtm_signal})"
        )
        print(f"  cpu={f.cpu or 'n/a'} memory={f.memory or 'n/a'} min={f.min_instances} max={f.max_instances}")
        avg7 = f"{f.avg_instances_7d:.2f}" if f.avg_instances_7d is not None else "n/a"
        avg30 = f"{f.avg_instances_30d:.2f}" if f.avg_instances_30d is not None else "n/a"
        print(f"  avg instances: 7d={avg7}  30d={avg30}")
        print(f"  Estimated monthly cost: {_fmt_eur(f.monthly_cost_eur)}  (confidence: {f.cost_confidence})")
        print(f"  Hostname: {f.hostname or 'unknown'}  (source: {f.hostname_source})")
        for note in f.notes:
            print(f"  note: {note}")

    total_ga4 = sum(f.monthly_cost_eur for f in ga4_findings)
    total_gtm = sum(f.monthly_cost_eur for f in gtm_findings)
    print("\n" + "=" * 100)
    print(f"TOTAL estimated GA4 BigQuery storage: {_fmt_eur(total_ga4)}/month")
    print(f"TOTAL estimated GTM SS hosting:       {_fmt_eur(total_gtm)}/month")
    print("=" * 100 + "\n")


def write_json(ga4_findings: List[GA4ExportFinding], gtm_findings: List[GTMHostingFinding], path: Path) -> None:
    payload = {
        "ga4_bigquery_export": [dataclasses.asdict(f) for f in ga4_findings],
        "gtm_hosting": [dataclasses.asdict(f) for f in gtm_findings],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))


def write_csv(ga4_findings: List[GA4ExportFinding], gtm_findings: List[GTMHostingFinding], output_dir: Path) -> None:
    ga4_path = output_dir / "ga4_bigquery_export.csv"
    with ga4_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "project_id", "account_email", "dataset_id", "property_id", "location",
                "storage_billing_model", "table_count", "oldest_table_date", "newest_table_date",
                "active_gib", "longterm_gib", "monthly_cost_eur", "cost_source", "notes",
            ]
        )
        for f in ga4_findings:
            writer.writerow(
                [
                    f.project_id, f.account_email, f.dataset_id, f.property_id, f.location,
                    f.storage_billing_model, f.table_count, f.oldest_table_date, f.newest_table_date,
                    round(f.active_bytes / (1024 ** 3), 3), round(f.longterm_bytes / (1024 ** 3), 3),
                    round(f.monthly_cost_eur, 2), f.cost_source, " | ".join(f.notes),
                ]
            )

    gtm_path = output_dir / "gtm_hosting.csv"
    with gtm_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "project_id", "account_email", "service_type", "service_name", "region",
                "is_likely_gtm_ss", "gtm_signal", "cpu", "memory", "min_instances", "max_instances",
                "cpu_always_allocated", "avg_instances_7d", "avg_instances_30d",
                "monthly_cost_eur", "cost_confidence", "hostname", "hostname_source", "notes",
            ]
        )
        for f in gtm_findings:
            writer.writerow(
                [
                    f.project_id, f.account_email, f.service_type, f.service_name, f.region,
                    f.is_likely_gtm_ss, f.gtm_signal, f.cpu, f.memory, f.min_instances, f.max_instances,
                    f.cpu_always_allocated, f.avg_instances_7d, f.avg_instances_30d,
                    round(f.monthly_cost_eur, 2), f.cost_confidence, f.hostname, f.hostname_source,
                    " | ".join(f.notes),
                ]
            )
