# GCP Cost Estimator — GA4 BigQuery Export & GTM Server-Side Hosting

A read-only audit script that, across as many Google accounts as you give it:

1. Finds every GCP project each account can see.
2. Finds any GA4 BigQuery Export dataset in each project, sizes it, and
   estimates the monthly storage cost in EUR.
3. Finds any Cloud Run service or App Engine app that looks like it could be
   hosting a Google Tag Manager server-side container, estimates its monthly
   hosting cost in EUR from its configuration and real recent usage, and
   works out what hostname it's actually serving.

It never writes to any GCP resource and never creates API keys or
service-account credentials — it only requests a read-only OAuth scope
against your own Google account(s).

## 1. One-time setup

1. In a Google Cloud project you control, enable these APIs (they only need
   to be enabled once, in one project — the OAuth client lives there, but it
   can then be used to inspect *any* project the signed-in account can see):
   - Cloud Resource Manager API
   - Cloud Billing API
   - BigQuery API
   - Cloud Run Admin API
   - App Engine Admin API
   - Compute Engine API
   - Cloud Monitoring API
2. Under "APIs & Services > Credentials", create an **OAuth client ID** of
   type **Desktop app**. Download the JSON and save it as
   `client_secret.json` in this directory (or point `client_secret_file` in
   your config at wherever you keep it — just don't commit it).
3. If your OAuth client is in "Testing" publishing status, add every Google
   account you plan to scan as a **test user** in the OAuth consent screen
   settings, or the consent flow will refuse to sign them in.
4. `pip install -r requirements.txt`
5. `cp config.example.yaml config.yaml` and fill in your accounts.

## 2. Running it

```bash
python -m gcp_cost_estimator.cli --config config.yaml
```

The first run opens a browser window once per account listed in
`config.yaml` for OAuth consent (read-only scope). Refresh tokens are then
cached under `tokens/`, so subsequent runs are non-interactive until a token
is revoked or expires.

Output:
- A console summary.
- `output/report.json` — everything, machine-readable.
- `output/ga4_bigquery_export.csv` and `output/gtm_hosting.csv`.

## 3. How the estimates work

### GA4 BigQuery export storage cost

GA4's linked export always creates a dataset named `analytics_<property_id>`
with one table per day (`events_YYYYMMDD`). The script lists every such
table and reads its stored byte count via BigQuery's table metadata API —
this is a metadata call, not a query, so it costs nothing and there's no
need to sample: every table's real size is read directly (datasets with
hundreds of tables are sampled from the most recent 60 and extrapolated,
purely to keep runtime sane, and this is called out in the notes).

Cost is worked out in this order of preference:
1. **Real billing data**: if the project (or any project reachable by the
   same account) has [billing export to
   BigQuery](https://cloud.google.com/billing/docs/how-to/export-data-bigquery)
   already configured, the script finds that export table and queries the
   actual BigQuery storage cost billed to the project. This is exact.
2. **List-price estimate**: otherwise, the script applies BigQuery's public
   storage pricing to the measured bytes — active storage rate for tables
   modified in the last 90 days, long-term (discounted) rate for older ones,
   minus the first 10 GiB/month free tier. See `pricing.py` for the exact
   numbers and links to the live pricing pages — **update them before
   quoting a client**, prices drift and this is a snapshot.

### GTM server-side hosting cost

For every Cloud Run service and App Engine version found, the script:
- Flags it as a likely GTM SS container based on the container image name
  (official image is `gcr.io/cloud-tagging-10302018/gtm-cloud-image`),
  known env vars, or common naming conventions (`stape`, `sgtm`, etc). This
  is a heuristic — anything not flagged should be checked manually, and
  anything flagged should still be sanity-checked.
- Reads its CPU/memory/min-instances/max-instances configuration directly.
- If min-instances equals max-instances (a fixed, non-autoscaling
  deployment), costs it directly off that fixed instance count — this case
  needs no usage data and is exact for the configuration given.
- Otherwise, pulls the real average instance count and request volume from
  Cloud Monitoring over the trailing 7 and 30 days and costs off the
  30-day average, since GTM SS traffic (and therefore autoscaled instance
  count) genuinely varies client to client.
- Applies Cloud Run's on-demand vCPU-second / GiB-second / per-request
  pricing (or App Engine's per-instance-hour pricing), noting the caveats
  around free tier being per-billing-account rather than per-service, and
  around CPU-only-allocated-during-requests billing being modeled only
  approximately.

### Hostname discovery

Checked in this order, stopping at the first match:
1. A direct Cloud Run domain mapping.
2. An external HTTPS Load Balancer with a serverless NEG backend pointing at
   the service — this is the standard pattern when GTM SS needs a
   first-party cookie domain or sits behind Cloud Armor/stape.io-style
   infrastructure; the custom domain lives on the load balancer's URL map,
   not on the Cloud Run service itself.
3. Falls back to the default `*.run.app` / `*.appspot.com` URL, flagged as
   such — if the real domain is fronted by something outside GCP entirely
   (e.g. a third-party CDN/reverse proxy), this script can't see it and it
   needs manual verification.

## 4. Limitations to keep in mind

- All prices are **public on-demand list prices**, not your actual
  negotiated/discounted rate. Treat every number as order-of-magnitude, not
  an invoice line.
- The `--fx-rate` / `fx_rate_usd_to_eur` conversion is a static number you
  supply, not a live rate.
- Requires the scanning account to have at least Viewer-level IAM access on
  each project to see anything in it.
- Cloud Run cost modeling doesn't fully replicate Google's idle-instance
  billing nuances when CPU is only allocated during request processing —
  see the per-service notes in the output for exactly where an estimate is
  approximate versus using real measured/billed data.
