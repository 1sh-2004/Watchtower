<img width="1440" height="814" alt="Screenshot 2026-11-15 at 7 20 43 PM" src="https://github.com/user-attachments/assets/7af39454-a8f8-4c93-b819-f189fe9cefa8" />#  WatchTower

A self-hosted operations monitor in Python that watches **your machine, your APIs and your AWS account** from one dashboard, raises de-duplicated alerts, and adds a small, explainable AIOps layer on top.

It started as a local CPU/RAM dashboard and was extended into an operations tool: background collection, API health and quota tracking, read-only AWS cost and waste detection, anomaly detection, Docker packaging, tests and CI.

## What it monitors

| Area | What you get |
|---|---|
| **Local system** | CPU, RAM, disk usage, **disk I/O**, **network up/down**, **NVIDIA GPU** (when `nvidia-smi` exists), top processes, 7-day history |
| **APIs and services** | Up/down, latency (current and p95), uptime %, HTTP errors, request counts, **rate-limit headers and remaining quota** when the provider sends them, **API key expiry reminders** |
| **AWS (read-only)** | EC2 inventory and state, S3 size and object counts, month-to-date spend, forecast and **budget thresholds**, **idle EC2**, unattached EBS volumes, unassociated Elastic IPs, **running instances in unexpected regions** |
| **Alerts** | Open → refresh → resolve lifecycle, de-duplicated, debounced in both directions (no flapping), optional webhook (Slack/Teams/generic) |
| **SLOs** | Per-service availability target, remaining **error budget**, and **burn-rate alerts** (fast and slow) instead of paging on single failures |
| **Incidents** | Related alerts grouped into one incident with a timeline and a ranked, explainable likely cause |
| **Prometheus / Grafana** | `/metrics` endpoint and a ready-made Grafana dashboard |
| **Insights** | Robust z-score anomaly detection, disk-full forecast, plain-English summary (rule-based by default, optional LLM) |

## Quick start

```bash
pip install -r requirements.txt
cp sentinel.example.yaml sentinel.yaml     # optional: add your services / enable AWS
python main.py                             # http://127.0.0.1:8000
```

With no config file it still monitors the local machine. Add services in `sentinel.yaml`; see the commented example.

### Docker

```bash
cp sentinel.example.yaml sentinel.yaml
docker compose up --build                  # http://127.0.0.1:8000
```

The container runs as a non-root user, with a read-only filesystem, all capabilities dropped, and a health check on `/api/health`. Note that inside a container the "local system" metrics describe the container's view of the host.

### Settings tab: add APIs, API keys and AWS keys from the dashboard

Open the **Settings** tab (from the machine Sentinel runs on) instead of editing YAML:

- **APIs and services:** name, URL, check interval, and optionally an API key (header, scheme such as `Bearer`, expiry date) and an availability goal. **Test once** runs one check and tells you whether the key was accepted and whether the API sends rate-limit (quota) headers. **Save** starts monitoring immediately; no restart. Services without a key are checked as open endpoints, side by side with the ones that have keys.
- **AWS:** regions, an access key ID and secret (plus a session token for temporary keys), budget and idle thresholds. **Test connection** makes free read-only calls (STS, EC2, S3, CloudWatch) and reports which permission is missing. Saving turns AWS monitoring on and runs the first collection at once. **Remove saved AWS settings** turns it off and deletes the keys.

With AWS off (the default) Sentinel makes no AWS calls and the Cloud tab says so.

Where keys live: in `sentinel.settings.json` next to the database, in plain text, readable only by your user (mode 0600) and git/docker-ignored. They are sent only to the service or to AWS, and no API response, log line or metric ever contains one; the dashboard only learns "key set". Anyone who can read that file as you can read the keys, so use read-only AWS keys and don't put the file in a shared backup. Settings endpoints refuse any request that is not from the local machine (the connection, the `Host` header and the `Origin` header are all checked). Services in `sentinel.yaml` are shown but can only be changed in that file; if a name exists in both, the YAML one wins.

### AWS from YAML or the environment (alternative)

1. Create an IAM user or role with [`infra/aws-readonly-policy.json`](infra/aws-readonly-policy.json). It contains only `Describe`/`List`/`Get` actions.
2. Provide credentials the normal AWS way (profile, environment variables, or instance role). **AWS keys never go in `sentinel.yaml`**; the file only holds `enabled`, `regions` and thresholds.
3. Set `aws.enabled: true` and your `regions` in `sentinel.yaml`.

Sentinel **never starts, stops, resizes or deletes anything**. Cost Explorer bills about $0.01 per request, so spend is polled every 6 hours by default. If AWS rejects a call, the Cloud tab shows the AWS error code (for example `UnauthorizedOperation`) with a plain-English hint; the error message itself is never stored because it can contain account details.

## SLOs and error budgets

Give a service a target and Sentinel tracks how much of its error budget is left:

```yaml
services:
  - name: Public website
    url: https://example.com
    slo:
      target: 99.9        # percent of checks that must succeed
      window_days: 7      # rolling window; cannot exceed retention_days
```
<img width="1440" height="814" alt="Screenshot 2026-11-15 at 7 20 43 PM" src="https://github.com/user-attachments/assets/6699ad60-a328-40ad-91ab-7d73f6739597" />

The SLI is the share of health checks that succeed. 99.9% over 7 days at one check per 30 s allows about 20 failed checks, and that count is the error budget. The **SLOs** tab shows availability, budget left, burn rates and a projected exhaustion time.

Alerts use the multiwindow, multi-burn-rate method from the Google SRE Workbook instead of firing on single failures. A *burn rate* of 1 spends the budget exactly over the window; 10 empties it ten times faster.

| Alert | Fires when | Severity |
|---|---|---|
| `slo:<service>:fast_burn` | 2% of the budget burned in 1 h, and still burning over the last 5 min | critical |
| `slo:<service>:slow_burn` | 5% of the budget burned in 6 h, and still burning over the last 30 min | warning |
| `slo:<service>:exhausted` | the budget for the window is spent | critical |

The thresholds are derived from the budget fraction, so a 30-day window gives the Workbook's 14.4x and 6x, and a 7-day window gives about 3.4x and 1.4x. Requiring both a long and a short window means an alert clears soon after recovery. At least 2 failed checks are also required, so a lone blip never pages. Logic and rationale: [`sentinel/slo.py`](sentinel/slo.py).

Limits: the SLI counts only checks Sentinel ran, so time when Sentinel itself was down is not counted against the service (the dashboard shows how full the window is). Availability only; there is no latency SLO yet.

## Incident grouping

One failure usually trips several alerts seconds apart: a CPU spike slows an API, checks start failing, the SLO starts burning. Sentinel groups alerts that open within `incidents.window_seconds` (default 300) of an incident's latest activity into **one incident** with a timeline. A flapping alert reopens its incident instead of creating a new one. API-key expiry and AWS cost/waste findings are housekeeping and are never grouped.

Each incident gets a **likely cause** from a transparent rule, not a model: host resource alerts rank below service alerts, which rank below SLO alerts (the user-facing impact); a resource alert counts as a candidate if it opened no more than 2 minutes after the first service symptom; the earliest candidate in the lowest layer wins. This reports correlation, and confidence is never above "medium". See [`sentinel/incidents.py`](sentinel/incidents.py). Grouping is by time, so a long-lived alert will not pull in an unrelated failure hours later.

API: `GET /api/incidents?status=` and `GET /api/incidents/{id}`.

## Prometheus and Grafana

`GET /metrics` serves Prometheus text format, read from current state at scrape time. Examples: `sentinel_service_up`, `sentinel_service_latency_seconds`, `sentinel_service_checks_total{result}`, `sentinel_slo_error_budget_remaining_ratio`, `sentinel_slo_burn_rate{window}`, `sentinel_alerts_open{severity,source}`, `sentinel_incidents_open`, plus CPU, memory, disk, throughput, GPU and AWS spend. Labels carry service names only; URLs, hosts and keys are never exported. Disable with `metrics.enabled: false`. Like the rest of the API it has no authentication.

```bash
docker compose --profile monitoring up --build
# Sentinel http://127.0.0.1:8000 · Prometheus http://127.0.0.1:9090 · Grafana http://127.0.0.1:3000 (admin/admin)
```

Grafana is provisioned with a data source and the [Sentinel overview dashboard](infra/grafana/dashboards/sentinel-overview.json) (SLI vs target, budget, burn rate, latency, system metrics). [`infra/prometheus/alerts.yml`](infra/prometheus/alerts.yml) holds optional Alertmanager rules. Validated: `promtool` accepts the config and rules, and a real Prometheus scraped Sentinel and answered every dashboard query. Not validated: the Grafana containers themselves, so check the dashboard renders on first run.

## Fault injection: measured detection time

[`tools/fault_inject.py`](tools/fault_inject.py) breaks things on purpose and times how long Sentinel takes to notice. Each trial starts a fresh Sentinel and a target service, waits for a healthy baseline, injects one fault, and polls Sentinel's own API until the expected alert appears, then removes the fault and times recovery.

```bash
python tools/fault_inject.py                         # 5 scenarios x 3 trials, about 5 minutes
python tools/fault_inject.py --service-interval 30   # Sentinel's production pace
```

Scenarios: kill the service process, return HTTP 500, slow the API by 800 ms, spike CPU on every core, spike disk I/O with heavy fsynced writes, a cascade (CPU spike and slow API together) and a full cascade (slow API, CPU and disk I/O together). Both cascades must end up as a single incident.

Measured with Sentinel checking every **1s** and alerting after **2** consecutive failures (system sampled every 1.0s, CPU alert after 3 samples over 60.0%, alerts clear after 3 calm samples). 3 trial(s) per scenario, median with range, polled every 100 ms.

| Fault injected | Alert Sentinel raised | Detected after | Cleared after | Grouping |
|---|---|---|---|---|
| Kill the service process (SIGKILL) | `svc:Target:down` | 2.0s (1.1-2.1s) | 0.8s (0.8-0.8s) |  |
| Kill the service process (SIGKILL) | `slo:Target:fast_burn` | 2.0s (1.1-2.1s) | n/a |  |
| Service starts returning HTTP 500 | `svc:Target:down` | 1.2s (1.1-2.1s) | 0.9s (0.8-0.9s) |  |
| Service starts returning HTTP 500 | `slo:Target:fast_burn` | 1.2s (1.1-2.1s) | n/a |  |
| API responds correctly but 800 ms slower | `svc:Target:slow` | 3.6s (2.7-3.6s) | 1.8s (1.8-1.8s) |  |
| CPU spike: busy loop on every core | `sys:cpu` | 5.2s (5.1-7.1s) | 4.0s (4.0-5.0s) |  |
| Disk I/O spike: sustained heavy writes | `sys:disk_io` | 2.6s (2.5-2.6s) | 3.2s (3.1-3.2s) |  |
| Slow API + CPU spike + disk I/O spike together | `sys:disk_io` | 2.5s (2.5-2.5s) | 3.2s (3.2-4.1s) | 1 incident; likely cause: sys:disk_io |
| Slow API + CPU spike + disk I/O spike together | `sys:cpu` | 4.9s (4.8-5.9s) | 5.1s (3.2-5.3s) |  |
| Slow API + CPU spike + disk I/O spike together | `svc:Target:slow` | 2.9s (2.9-3.1s) | 2.0s (1.9-2.6s) |  |
| CPU spike and slow API together | `sys:cpu` | 7.1s (6.2-7.2s) | 4.9s (2.9-5.1s) | 1 incident; likely cause: sys:cpu |
| CPU spike and slow API together | `svc:Target:slow` | 3.6s (2.7-3.8s) | 2.6s (1.9-2.9s) |  |

Run on Linux 6.18.44-fc-v470, Python 3.12.3, 1 CPU(s), 2026-11-15. **Read the numbers with the configuration above:** detection time is mostly check interval times failures before alerting, so a fast configuration detects in seconds and the production default (30 s checks, 2 failures) takes about a minute. SLO alerts are timed for detection only; their short window is at least 5 minutes, so they clear long after a trial ends. The cascades inject their faults together to test grouping; it does not prove causal inference. Re-run it on your own machine and replace this table with your numbers.

## Design decisions

- **Collection runs in the background**, not on page load. The original only stored metrics while a browser tab was polling, so history had gaps and alerts could not fire unattended.
- **Alerts have a lifecycle.** One open alert per key, notified only when it opens or resolves. Breaches must be *sustained* (N consecutive samples) and services must fail N times before paging, which keeps noise down.
- **Alerts resolve with hysteresis too.** CPU, memory, disk, disk I/O and "API slow" alerts clear only after `thresholds.resolve_samples` (default 3) consecutive calm samples, so a metric hovering near its threshold does not open and close an alert every few seconds. An alert that was open when Sentinel restarted stays open until it has genuinely been calm. "Service down" still resolves on the first good check, because fast recovery news matters more there; incident grouping absorbs any flapping.
- **Disk I/O alerts on change, not on a ceiling.** Disk throughput has no natural 100%, so `sys:disk_io` fires when read+write throughput stays far above its own recent baseline (robust z-score, same maths as the anomaly detector) *and* the jump is at least `disk_io_min_mbps`. The baseline is the last ~120 samples kept in memory, so it rebuilds after a restart (about 20 samples) and a spike that lasts for minutes eventually becomes the new normal.
- **Failures are isolated.** If Cost Explorer is denied, EC2 and S3 still report. A failed collector never kills its loop. Partial AWS data never auto-resolves existing alerts.
- **Capacity forecasts say nothing when unsure.** A trend needs enough points and r² ≥ 0.5, otherwise no forecast is shown.

## Security and responsible AI

- **Secrets are never logged or returned.** In `sentinel.yaml` the config holds only the *name* of an environment variable; a literal key in the config is rejected at startup. Keys typed into the Settings tab are the one place a secret is stored: a local file readable only by your user (see above). Error text records only exception classes and AWS error codes, because AWS and HTTP error messages can contain account IDs or URLs. Tests assert that no API response contains a key.
- **Service URLs are never returned by the API**, only the host, since URLs can carry tokens.
- **The dashboard builds the DOM with `textContent`**, never `innerHTML`, so process names or alert text cannot inject markup. Chart.js is bundled locally: no third-party scripts, and the app works offline.
- **Binds to `127.0.0.1` by default** and has no login. The Settings endpoints additionally refuse non-local requests; the read-only dashboard endpoints do not, so if you expose the dashboard, put a reverse proxy with authentication in front.
- **The LLM summary is off by default (opt-in)** and advisory. It receives only counts and numbers: no service names, hostnames, resource IDs or alert text. If it is unavailable, the rule-based summary is used. Anomaly detection and forecasting are plain statistics you can read in [`sentinel/aiops.py`](sentinel/aiops.py).

## Architecture

```
main.py → sentinel/app.py (FastAPI)
                │
         sentinel/runtime.py ── background loops ──┬─ collectors/system.py    psutil, nvidia-smi
                │                                  ├─ collectors/services.py  httpx health checks
                │                                  └─ collectors/aws.py       boto3, read-only
                ├─ settings.py keys/services entered in the dashboard (0600 file)
                ├─ alerts.py   lifecycle, debounce, webhook
                ├─ slo.py      error budgets, burn-rate alerts
                ├─ incidents.py  alert grouping, likely cause
                ├─ metrics.py  Prometheus /metrics
                ├─ aiops.py    z-score, forecast, summaries
                └─ db.py       SQLite (WAL), retention pruning
static/  dashboard (vanilla JS + bundled Chart.js)
tools/   fault_inject.py
infra/   AWS policy, Prometheus config and rules, Grafana provisioning and dashboard
```

### API

`GET /api/health` · `/api/settings` (+ `PUT/DELETE` services and AWS, `POST /test/...`; localhost only) · `/api/system/{current,processes,history?hours=}` · `/api/services` · `/api/services/{name}/latency` · `/api/cloud/aws` · `/api/slo` · `/api/incidents` · `/api/alerts?status=` · `/api/insights` · `/metrics`. Interactive docs at `/docs`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

111 tests cover config validation, rate math, GPU parsing, API checks, secret redaction, the alert lifecycle, AWS findings, anomaly and forecast maths, LLM minimisation, the HTTP API, the Settings tab (key handling, localhost-only guard, AWS hot-swap, persistence), SLO budget and burn-rate maths, incident grouping and cause ranking, the Prometheus endpoint (parsed with the official parser), alert hysteresis and restart safety, the disk I/O anomaly alert and the fault-injection helpers. AWS tests use fake clients; no credentials or network needed. CI runs the suite on Python 3.10 and 3.12 and smoke-tests the Docker image.

## Known limitations

- GPU metrics support NVIDIA only.
- S3 size uses the `StandardStorage` CloudWatch metric, so other storage classes are not counted.
- "Request counts" are the checks Sentinel makes, not your application's real traffic.
- Single-node only; no authentication built in (this includes `/metrics`). Keys saved from the dashboard are plain text in a 0600 file, not encrypted.
- Editing a dashboard service keeps its name; to rename one, remove and re-add it (history is kept by name).
- Settings changes apply immediately, but `sentinel.yaml` is still read only at startup.
- AWS was tested here against a mock AWS (moto) and fakes, not a real account: run **Test connection** with your read-only keys first. Cost Explorer needs to be enabled once in your AWS console.
- "Service down" and "quota low" alerts still resolve on the first good check (system, disk I/O and "API slow" alerts use hysteresis).
- The disk I/O baseline lives in memory and adapts, so it flags sudden jumps rather than steady heavy I/O, and it needs ~20 samples after a restart before it can judge.
- Incident grouping is by time only; there is no service dependency map, so the likely cause is a heuristic.
- SLOs, incidents and `/metrics` are tested, and Prometheus was run against a live Sentinel. The Grafana containers and the compose `monitoring` profile have not been run.

## Ideas for next steps

Latency SLOs · incident-level webhook notifications · Terraform for the IAM role and deployment · Kubernetes manifests · ITSM/CMDB export · multi-host agents.

## License

Open source. Use freely.
