# zpa-collector — Deployment Guide

> For Zscaler SEs **and** customer admins. Stand up the container, point ZPA LSS
> at it, and pull an advisory "review these segments / rules / servers" report.

A single, stdlib-only, zero-pip container that receives the ZPA LSS
`zpn_trans_log` (User Activity) stream, keeps per-object usage counters (no raw
logs stored), diffs them against **read-only** OneAPI config, and serves a
hygiene dashboard + CSV.

It accumulates a **long observation horizon** (a quarter, ideally a fiscal year)
so a "no hits" verdict means something the native ~14-day usage report cannot.
Everything it emits is **advisory** — Red/Silent means *review*, never *delete*.

> **⚠️ Unofficial — not a Zscaler product.** A personal project; **not** built,
> supported, endorsed, or reviewed by Zscaler. Vibe-coded with the help of Claude
> (Opus). It talks to ZPA **read-only** and never writes to your tenant, but it's
> provided **as-is, with no warranty** — validate it in a lab and read the
> [methodology caveat](#methodology-caveat-read-before-acting-on-candidates) before
> acting on anything it reports. MIT licensed.

---

## Prerequisites

Line these up before you start — the **TLS cert has the longest lead time**:

- **Host:** a Linux box with **Docker + Docker Compose**. Footprint is small —
  ~1 vCPU, ~256 MB RAM, well under 1 GB disk (state is a few MB).
- **Network:** your ZPA **App Connectors must reach `<collector-host>:4639/tcp`**
  (the LSS TLS receiver). You reach the dashboard at `<collector-host>:8866/tcp`.
  Open firewall + routing for both paths.
- **TLS cert:** a **public-CA-signed** cert + key for a DNS name the connectors
  will use (a hard ZPA requirement — see Step 3). Request/issue it early.
- **Access:** **ZIdentity admin** (to create a read-only API client) and **ZPA
  admin** (to create the LSS config).
- **Decide** the hostname the App Connectors will use to reach the collector.

---

## Step 0 — Get the collector

```bash
git clone https://github.com/ivancurrey/zpa-collector
cd zpa-collector
cp .env.example .env
```

## Step 1 — Create a read-only OneAPI client + fill in `.env`

The container talks to OneAPI **read-only** (config-read role only) and never
writes to ZPA — which is exactly why it **cannot** create the LSS config for you
(that's the manual Step 4).

1. In **ZIdentity**, create an API client (client-credentials) whose role is
   limited to **config read** for ZPA.
2. Note your vanity domain (e.g. `acme` → `acme.zslogin.net`), client id, client
   secret, and your ZPA customer id.
3. Edit `.env`:
   - `ZS_VANITY_DOMAIN`, `ZS_CLIENT_ID`, `ZS_CLIENT_SECRET`, `ZPA_CUSTOMER_ID`
   - `DASH_TOKEN` — any strong random string; it gates the dashboard / API / CSV.

Docs: [OneAPI — Getting Started](https://help.zscaler.com/oneapi/getting-started) · [ZIdentity — API Clients](https://help.zscaler.com/zidentity/api-clients) · [OneAPI Authentication](https://help.zscaler.com/zidentity/understanding-oneapi-authentication).

## Step 2 — Provide the receiver TLS cert (public-CA — required)

The receiver needs a cert/key at `LSS_CERT_PATH` / `LSS_KEY_PATH` (default
`/data/receiver.crt` / `/data/receiver.key`).

> ⚠️ **The cert MUST be signed by a public root CA** (or by a custom CA that is
> the App Connector's *enrollment* CA). This is a hard ZPA requirement — the App
> Connector validates the receiver's certificate chain. A **self-signed** cert is
> rejected with a TLS **"Unknown CA"** handshake failure and **no logs flow**.
> Ref: [Zscaler LSS TLS requirements](https://help.zscaler.com/zpa/about-log-streaming-service).

Two supported options:

1. **Public-CA / ACME cert (simplest).** Use a cert from a public CA (Let's
   Encrypt, ZeroSSL, …) for a name you control. If you already terminate TLS for
   a domain (e.g. a reverse proxy with an ACME wildcard), reuse that `fullchain`
   + key as `receiver.crt` / `receiver.key`.
   - **Renewal:** public certs are short-lived (~90 days) and the copy on the
     volume does **not** auto-renew. Re-copy the renewed cert + `docker compose
     restart` before each expiry (an **expired** cert fails the same way,
     silently). Check expiry any time:
     `docker exec zpa-collector python -c "import ssl;print(ssl._ssl._test_decode_cert('/data/receiver.crt')['notAfter'])"`.
2. **Custom-CA mTLS (long-lived, advanced).** Sign the receiver cert with the App
   Connector's [enrollment CA](https://help.zscaler.com/zpa/about-enrollment-ca-certificates)
   (Zscaler's mTLS path) — more setup, no 90-day treadmill.

The connector enforces **CA trust, not hostname**, so the cert SAN need not match
the LSS destination; still, using a DNS name the cert covers (not a bare IP) is
the cleaner config.

**Load the cert into the data volume** (with `receiver.crt` / `receiver.key` in
the current directory):

```bash
docker compose up -d                      # creates the data volume; the app will
                                          # crash-loop on the missing cert — expected
VOL=$(docker volume ls -q | grep zpa_collector_data | head -1)
docker run --rm -v "$VOL:/data" -v "$PWD:/src:ro" busybox \
  sh -c 'cp /src/receiver.crt /src/receiver.key /data/ \
         && chown 10001:10001 /data/receiver.* && chmod 600 /data/receiver.key'
```

## Step 3 — Start the collector

```bash
docker compose restart      # (or `docker compose up -d` on a fresh host)
docker compose logs -f zpa-collector
```

This binds **`:4639`** (LSS TLS receiver — point the LSS config here) and
**`:8866`** (dashboard / API / CSV, token-gated). State and the cert live on the
`zpa_collector_data` volume.

## Step 4 — Create the LSS config in ZPA (manual)

The read-only key can't do this for you. In the ZPA admin console, create a Log
Streaming Service (LSS) configuration:

- **Log type:** **User Activity** (`zpn_trans_log`).
- **JSON template** — must include **exactly** these fields (a wrong template is
  the #1 "connected but no data" cause):

  ```
  Application
  AppGroup
  Policy
  Server
  ServerIP
  Host
  Connector
  ConnectionID
  ConnectionStatus
  Username
  TimestampConnectionStart
  TimestampConnectionEnd
  ```

  The canonical template is at
  `…/v2/lssConfig/logType/formats?logType=zpn_trans_log`; use it verbatim.

- **TLS:** **on**.
- **Destination:** `<collector-host>:4639`.
- **Bind to ALL relevant connector groups.** Under-binding is a silent
  false-retire: segments served by an **unbound** connector group look 100%
  unused and show up as retire candidates that are actually in heavy use. Bind
  every connector group that serves the segments you care about.

Docs: [Configuring a Log Receiver](https://help.zscaler.com/zpa/configuring-log-receiver) · [Understanding User Activity Log Fields](https://help.zscaler.com/zpa/understanding-user-activity-log-fields).

## Step 5 — Verify it's working

**Quick check (no extra hosts needed)** — `/health` is open (no token):

```bash
curl -s http://<collector-host>:8866/health
```

You want `"state":"OK"`, **`active_conns` ≥ 1** (your App Connectors are
attached), and **`records_total` climbing** as ZPA user traffic flows. If
`active_conns` is 0 or records stay at 0, see **Troubleshooting**.

**Built-in self-test (loopback — proves the parse→count→CSV path end to end):**

```bash
docker compose exec zpa-collector python -m collector --selftest
```

Prints `PASS` (exit 0) or `FAIL` (exit 1).

**(Advanced) Synthetic LSS sender** — tests the real network path; run it from a
host on the **connector subnet** that has this repo checked out (Python 3.11+,
no install):

```bash
python -m collector synth-send --host <collector-host> --port 4639
python -m collector synth-send --host <collector-host> --port 4639 --no-tls   # framing only
```

It sends a brokered open+close pair (deduped to one) plus a self-conn line
(dropped); refresh the dashboard and the demo segment should show a single hit.

## Step 6 — Use it

- Open `http://<collector-host>:8866/`, paste the `DASH_TOKEN`, and **Refresh**.
- Adjust **WARM / STALE / WINDOW** live — heat bands recompute at render time; the
  `.env` values are only defaults.
- Pull the deliverable: **`/export.csv`** (thresholds + horizon as query params).
  The CSV header stamps coverage % over N days with G gaps, the thresholds used,
  and the methodology caveat. **Usernames are never exported** (UI drill-down
  only).
- Force an immediate config sync (otherwise nightly): `POST /api/sync` with the
  token.

---

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| Container won't stay up; log says cert/key not found | Cert not loaded onto the volume → **Step 2**. |
| Connector/TLS shows **"Unknown CA"**; `active_conns` stays 0 | Receiver cert isn't **public-CA-signed** (self-signed fails) → **Step 2**. |
| `/health` `active_conns: 0` | Connectors can't reach `<host>:4639` (firewall / routing / DNS) or the LSS **Destination** is wrong → **Network prereq** + **Step 4**. |
| `/health` returns **non-200** / "template mismatch" | The LSS JSON template is missing required fields → **Step 4**. |
| `active_conns ≥ 1` but `records_total: 0` | No ZPA user traffic yet, or only self-connections — generate some access through a segment, then recheck. |
| Worked, then TLS fails later | The public cert **expired** (~90-day) — re-copy the renewed cert + restart → **Step 2 renewal**. |
| Dashboard cards/tables empty after Refresh | Wrong/blank `DASH_TOKEN`, or no config synced yet — check the token and `POST /api/sync`. |

Deeper LSS diagnostics: [Zscaler LSS Troubleshooting Runbook](https://help.zscaler.com/troubleshooting-runbooks/zscaler-log-streaming-service-lss-troubleshooting-runbook).

## Methodology caveat (read before acting on candidates)

- The window can't detect a cadence **longer than the horizon** (quarter-end,
  payroll, DR/failover, audit season). Use `DONOTRETIRE_LIST` for known-periodic
  objects.
- Policy **rule shadowing** (a rule shadowed by an earlier allow, or a
  break-glass/DR rule) never appears in the `Policy` field — a Silent **rule** is
  a review prompt, not a delete order.
- **Dead-server detection is low-confidence** (load-balanced / NAT'd /
  DNS-round-robin backends log unstable `Server`/`ServerIP`).
- LSS is **best-effort** and per-log-type / per-connector-group scoped. The CSV
  header stamps **coverage % over N days with G gaps** — judge whether the window
  is trustworthy before acting; a gappy window means a "Silent" object may simply
  have been missed, not unused.

## Configuration reference

See `.env.example` for every variable and its default.

> **v1 note:** `DONOTRETIRE_LIST` is applied (allowlisted names are excluded from
> candidates). `CONNECTOR_GROUP_FILTER` (report scoping) and `HEALTH_WEBHOOK`
> (outbound alerts) are **reserved and not yet wired** — loud alerting today is
> the dashboard banner, the non-200 `/health` (for the container `HEALTHCHECK` /
> uptime probes), and ERROR logs.

## References

- [Zscaler OneAPI — Getting Started](https://help.zscaler.com/oneapi/getting-started) · [OneAPI Authentication](https://help.zscaler.com/zidentity/understanding-oneapi-authentication) · [ZIdentity — API Clients](https://help.zscaler.com/zidentity/api-clients)
- [About the Log Streaming Service](https://help.zscaler.com/zpa/about-log-streaming-service) — incl. the TLS cert requirements · [Configuring a Log Receiver](https://help.zscaler.com/zpa/configuring-log-receiver)
- [Understanding User Activity Log Fields](https://help.zscaler.com/zpa/understanding-user-activity-log-fields) — the `zpn_trans_log` template this tool parses
- [App Connector Enrollment](https://help.zscaler.com/zpa/about-deploying-connectors) · [Enrollment CA Certificates](https://help.zscaler.com/zpa/about-enrollment-ca-certificates) — for the custom-CA mTLS cert path
- [LSS Troubleshooting Runbook](https://help.zscaler.com/troubleshooting-runbooks/zscaler-log-streaming-service-lss-troubleshooting-runbook)

---

## Appendix — how it works

- **Count-and-drop:** each LSS record is parsed for the fields above, counted,
  and the payload **discarded** — no raw logs are ever stored. The only personal
  data retained is a small, bounded **username sample** (drill-down only, never
  in the CSV).
- **Dedup:** a connection emits an open **and** a close record sharing a
  `ConnectionID`; they're collapsed to one count. Counts are advisory — the
  keep/retire signal is **presence** (was it seen) and **recency** (`last_seen`).
- **id-keyed:** usage is keyed to the resolved ZPA object **id** (the log name is
  resolved to an id at ingest), so renaming an object in ZPA never produces a
  false retirement.
- **Read-only OneAPI:** the config sync only ever GETs; it pages results, paces
  to ZPA's rate limit, retries 429s, and iterates microtenants (it never sends
  `microtenantId=null`, which returns HTTP 400).
- **Drift:** `retired` = was in config before, gone now, but had usage; `ghost` =
  usage seen for something not in current config (rare by design).
