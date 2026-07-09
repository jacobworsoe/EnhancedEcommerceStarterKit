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
against your own Google account(s). There are two ways to run it: a CLI
(`cli.py`) or a small local web app (`webapp/app.py`) with a browser-based
OAuth flow, a live progress log, and results tables — same scan, same
package underneath, pick whichever fits.

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
2. Under "APIs & Services > Credentials", create an OAuth client ID:
   - For the **CLI**: type **Desktop app**.
   - For the **web app**: type **Web application**, with an authorized
     redirect URI of `http://localhost:8765/oauth2callback` (or whatever
     `redirect_uri`/`port` you set in `config.yaml`).
   Download the JSON and save it as `client_secret.json` in this directory
   (or point `client_secret_file` in your config at wherever you keep it —
   just don't commit it).
3. If your OAuth client is in "Testing" publishing status, add every Google
   account you plan to scan as a **test user** in the OAuth consent screen
   settings, or the consent flow will refuse to sign them in.
4. `pip install -r requirements.txt`
5. `cp config.example.yaml config.yaml` and fill in your values.

## 2. Running it — CLI

```bash
python -m gcp_cost_estimator.cli --config config.yaml
```

The first run opens a browser window once per account listed under
`accounts:` in `config.yaml` for OAuth consent (read-only scope). Refresh
tokens are then cached under `tokens/`, so subsequent runs are
non-interactive until a token is revoked or expires.

Output:
- A console summary.
- `output/report.json` — everything, machine-readable.
- `output/ga4_bigquery_export.csv` and `output/gtm_hosting.csv`.

## 2b. Running it — local web app

```bash
python -m webapp.app --config config.yaml
```

Then open **http://localhost:8765/** (use `localhost`, not `127.0.0.1` —
it has to match whatever host is in the registered redirect URI). From
there:

1. Click **"+ Connect a Google account"** once per account (client,
   colleague, your own) — this redirects to Google's real consent screen
   and back, then lists the account as connected. Repeat for every board
   member's account; each one is saved to the same `tokens/` cache the CLI
   uses, so accounts connected here also work with the CLI and vice versa.
2. Set the FX rate / billing-export lookback if you want something other
   than the config defaults, and click **Start scan**.
3. Watch the live log while it runs, then browse the GA4 and GTM results
   tables, or download the same CSV/JSON the CLI produces.

Notes:
- The server binds to `127.0.0.1` by default (see `host` in
  `config.yaml`) — it is not reachable from your network unless you
  deliberately change that.
- It sets `OAUTHLIB_INSECURE_TRANSPORT=1` so Google's OAuth library accepts
  the plain-HTTP `localhost` redirect. That is safe only because the app
  isn't exposed beyond localhost — don't reuse this setting for anything
  actually deployed on a network.
- The Flask session secret is regenerated every process start, so if you
  restart the server mid-consent-flow, just click connect again.
- Scans run in a background thread; only one at a time (starting a second
  while one's running returns an error rather than queuing).

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
1. **Real billing data**: if [billing export to
   BigQuery](https://cloud.google.com/billing/docs/how-to/export-data-bigquery)
   is configured for the project's billing account, the script queries the
   actual BigQuery storage cost billed to the project. This is exact.
2. **List-price estimate**: otherwise, the script applies BigQuery's public
   storage pricing to the measured bytes — active storage rate for tables
   modified in the last 90 days, long-term (discounted) rate for older ones,
   minus the first 10 GiB/month free tier. See `pricing.py` for the exact
   numbers and links to the live pricing pages — **update them before
   quoting a client**, prices drift and this is a snapshot.

### Multiple accounts, overlapping project access, and uneven billing visibility

It's normal for several accounts in `config.yaml` to see the same project,
and for only some of them to have Billing Account Viewer (or wherever the
billing export lives). To make sure that overlap actually helps rather than
just producing duplicate rows or missing an available real cost:

- Every account's project list is discovered independently first. A project
  seen by more than one account is scanned once (the account with billing
  enabled is preferred for the scan itself), and every other account that
  can also see it is recorded (`also_visible_via`) rather than discarded.
- Separately, the script scans **every** project **every** account can see
  for a billing export table (a free metadata-only scan) and builds one
  registry of `billing_account_id -> export table`, keyed off the billing
  account rather than off any particular project or account. That means if
  even one board's account can see the shared billing-export project, every
  other project on that same billing account gets priced from real data —
  regardless of which account happened to be used to scan that project's
  GA4 datasets or Cloud Run services.
- As a final safety net (in `dedupe.py`), the full finding list is grouped
  by resource (project+dataset, or project+service) before the report is
  written, and where a duplicate exists the entry with a real billing-export
  cost always wins over one that's only an estimate — so mixed billing
  visibility across boards can never accidentally leave an estimate in the
  report when a real number was available from another account.

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
- Same billing-export preference as GA4: if the project's billing account
  has a reachable export table, the real Cloud Run/App Engine cost replaces
  the estimate. The export isn't broken out per-service, so if a project
  has more than one candidate service the real total is split across them
  by their estimated share and flagged as `billing_export-prorated` rather
  than presented as exact per-service.

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
