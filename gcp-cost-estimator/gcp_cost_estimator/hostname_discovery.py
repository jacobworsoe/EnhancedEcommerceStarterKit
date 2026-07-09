"""
Work out which hostname a Cloud Run service or App Engine app is actually
serving traffic on.

There are three ways a GTM server-side container ends up reachable on a
client's real domain, in roughly descending order of how common they are in
practice:

1. Cloud Run "domain mapping" (`gcloud run domain-mappings`) - the simplest
   setup, directly maps a custom domain to the service.
2. An external HTTPS Load Balancer with a serverless NEG backend pointing at
   the Cloud Run service - the standard pattern when the container needs a
   first-party cookie domain, Cloud Armor, or is behind stape.io-style
   infra. The custom domain lives on the load balancer's URL map host
   rules, not on the Cloud Run service itself.
3. Nothing custom configured - the service is only reachable on its default
   *.run.app / *.appspot.com URL.

We check them in that order and stop at the first match.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def cloud_run_domain_mapping(run_v1: Resource, project_id: str, service_name: str) -> Optional[str]:
    try:
        resp = run_v1.namespaces().domainmappings().list(parent=f"namespaces/{project_id}").execute()
    except HttpError as exc:
        logger.debug("Domain mapping list failed for %s: %s", project_id, exc)
        return None

    for mapping in resp.get("items", []):
        route_name = mapping.get("spec", {}).get("routeName")
        if route_name == service_name:
            return mapping.get("metadata", {}).get("name")
    return None


def _neg_matches_cloud_run_service(compute: Resource, project_id: str, neg_self_link: str, service_name: str) -> bool:
    # self_link looks like .../zones/<zone>/networkEndpointGroups/<name> or
    # .../regions/<region>/networkEndpointGroups/<name>
    try:
        parts = neg_self_link.split("/")
        neg_name = parts[-1]
        if "regions" in parts:
            region = parts[parts.index("regions") + 1]
            neg = compute.regionNetworkEndpointGroups().get(
                project=project_id, region=region, networkEndpointGroup=neg_name
            ).execute()
        else:
            zone = parts[parts.index("zones") + 1]
            neg = compute.networkEndpointGroups().get(
                project=project_id, zone=zone, networkEndpointGroup=neg_name
            ).execute()
    except (HttpError, ValueError, IndexError) as exc:
        logger.debug("Could not resolve NEG %s: %s", neg_self_link, exc)
        return False

    cloud_run_ref = neg.get("cloudRun", {})
    return cloud_run_ref.get("service") == service_name


def load_balancer_hostnames_for_cloud_run(compute: Resource, project_id: str, service_name: str) -> List[str]:
    """Best-effort scan of URL maps for a host rule that routes to a
    serverless NEG backend pointing at this Cloud Run service."""
    hostnames: List[str] = []
    try:
        url_maps = compute.urlMaps().list(project=project_id).execute().get("items", [])
    except HttpError as exc:
        logger.debug("Cannot list urlMaps in %s: %s", project_id, exc)
        return hostnames

    for url_map in url_maps:
        matched_path_matchers = set()

        backend_services_to_check = set()
        if url_map.get("defaultService"):
            backend_services_to_check.add(url_map["defaultService"])
        for pm in url_map.get("pathMatchers", []):
            if pm.get("defaultService"):
                backend_services_to_check.add(pm["defaultService"])

        matched_backend_services = set()
        for backend_service_link in backend_services_to_check:
            try:
                name = backend_service_link.split("/")[-1]
                is_regional = "/regions/" in backend_service_link
                if is_regional:
                    region = backend_service_link.split("/regions/")[1].split("/")[0]
                    bs = compute.regionBackendServices().get(
                        project=project_id, region=region, backendService=name
                    ).execute()
                else:
                    bs = compute.backendServices().get(project=project_id, backendService=name).execute()
            except HttpError:
                continue

            for backend in bs.get("backends", []):
                group = backend.get("group", "")
                if "networkEndpointGroups" in group and _neg_matches_cloud_run_service(
                    compute, project_id, group, service_name
                ):
                    matched_backend_services.add(backend_service_link)

        if not matched_backend_services:
            continue

        if url_map.get("defaultService") in matched_backend_services:
            for host_rule in url_map.get("hostRules", []):
                hostnames.extend(host_rule.get("hosts", []))

        for pm in url_map.get("pathMatchers", []):
            if pm.get("defaultService") in matched_backend_services:
                matched_path_matchers.add(pm["name"])
        for host_rule in url_map.get("hostRules", []):
            if host_rule.get("pathMatcher") in matched_path_matchers:
                hostnames.extend(host_rule.get("hosts", []))

    return sorted(set(hostnames))


def app_engine_domain_mappings(appengine, app_id: str) -> List[str]:
    try:
        resp = appengine.apps().domainMappings().list(appsId=app_id).execute()
    except HttpError as exc:
        logger.debug("App Engine domain mapping list failed for %s: %s", app_id, exc)
        return []
    return [dm.get("id") for dm in resp.get("domainMappings", []) if dm.get("id")]
