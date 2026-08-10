# Creating the Dynatrace credentials this repo needs

Three credentials, three places to create them, three header formats. Mixing them
up produces a 401 that reads like an ingest failure, so this page has the exact
click-path for each.

Verified against Dynatrace docs on 2026-08-04. Links at the bottom.

| # | Credential | Where you create it | Header | Used by |
|---|---|---|---|---|
| 1 | **Access token** (environment) | Environment UI → Access Tokens | `Api-Token dt0c01…` | OTLP ingest, Metrics API read |
| 2 | **Platform token** | `myaccount.dynatrace.com/platformTokens` | `Bearer dt0s16…` | dashboard import |
| 3 | **OAuth client** | Account Management → Identity & access management → OAuth clients | `Bearer` (exchanged) | dashboard import from CI |

For a laptop or a single deployment, you need **#1** and **#2**. Reach for **#3**
only when a pipeline needs credentials that aren't tied to your user.

---

## 1. Access token — OTLP ingest and metric read

This is the `Api-Token` used by the router (or the collector) to push telemetry,
and by `scripts/verify_ingest.sh` to read it back.

1. In your **environment** (not Account Management), open the app switcher and
   search for **Access Tokens**. Direct URL:
   `https://<env-id>.apps.dynatrace.com/ui/apps/dynatrace.classic.tokens`
   (classic UI: **Settings → Access tokens → Generate new token**).
2. **Generate new token**.
3. **Token name**: something you'll recognise later, e.g. `apollo-router-otlp`.
   Dynatrace does not enforce unique names, so a vague name becomes unmanageable.
4. Select these scopes — search the scope list by the API value:

   | Scope (API value) | Why |
   |---|---|
   | `openTelemetryTrace.ingest` | traces |
   | `metrics.ingest` | metrics |
   | `logs.ingest` | log events |
   | `metrics.read` | so `verify_ingest.sh` can read the data back |

   Ingest and read are separate scopes. A token with only `metrics.ingest` will
   push fine and then 401 on verification — which reads like a broken pipeline.
5. **Generate token**, then copy it. **It is shown once.** Put it in
   1Password/`.env`, not in a config file.
6. Fill in `.env`:

   ```bash
   DYNATRACE_API_TOKEN=dt0c01.XXXXXXXX.YYYYYYYY   # direct topology
   DT_API_TOKEN=dt0c01.XXXXXXXX.YYYYYYYY          # collector topology + verify
   ```

**Check it works** (replace the env id):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Api-Token $DYNATRACE_API_TOKEN" \
  "https://<env-id>.live.dynatrace.com/api/v2/metrics?pageSize=1"
# 200 = token + metrics.read OK · 401 = bad token · 403 = missing scope
```

---

## 2. Platform token — dashboard import

Platform tokens are the current replacement for personal access tokens. They are
created in **Account Management**, not in the environment, and they carry a
`Bearer` header. Prefix: `dt0s16`.

1. Go to **`https://myaccount.dynatrace.com/platformTokens`** (bookmark it — it is
   not where the environment's Access Tokens live).
2. Select **Platform token** and fill in:
   - **Token Name** — e.g. `apollo-dashboard-import`
   - **Expiration date** — pick one; "never" is allowed but not advisable
   - **Account** — the account that owns the environment
   - **Apply to account** or **Environments** — scope it to just the environment
     you're importing into
3. Choose whether the token belongs to you (default) or to a **service user** you
   have access to. Use a service user for anything shared.
4. Select the scope:

   | Scope | Why |
   |---|---|
   | `document:documents:write` | create the dashboard document |
   | `document:documents:read` | optional — lets you list/verify afterwards |

5. **Generate**, copy the token (shown once), then **Finish and exit**.
6. Fill in `.env`:

   ```bash
   DT_BEARER_TOKEN=dt0s16.XXXXXXXX.YYYYYYYY
   ```

**Two gotchas:**

- A platform token only works **within the permissions of the user it belongs
  to**. Selecting a scope does not grant access the user doesn't already have —
  this is the usual cause of a 403 when the scope looks right.
- Limits: 10 platform tokens per user per account; one account only; expired
  tokens return **403**, not 401.

**Check it works:**

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $DT_BEARER_TOKEN" \
  "https://<env-id>.live.dynatrace.com/platform/document/v1/documents?filter=type%3D'dashboard'"
# 200 = ready to import · 403 = scope present but user lacks the permission
```

Then: `./scripts/import_dashboard.sh`

---

## 3. OAuth client — CI / service-account imports

Only needed when the import runs unattended. Client-credentials grant, no user
in the loop.

1. Go to **`https://myaccount.dynatrace.com/`** → top nav **Identity & access
   management** → **OAuth clients** → **Create client**.
2. **Grant type**: **Client credentials**.
3. **Subject user email**: an active user — a **service user** is the right
   answer here, or any user with account-user-management permission.
4. **Description**: what the client is for (up to 255 chars).
5. Select the permission **`document:documents:write`** (plus
   `document:documents:read` if you want to verify).
6. **Create client**, then copy **client ID** *and* **client secret** — the secret
   is shown once. Client IDs look like `dt0s02.ABCDE123` / `dt0s08…`.
7. You also need the **account UUID** — visible in the Account Management URL, or
   under **Account settings**. The token request is scoped by it.
8. Fill in `.env`:

   ```bash
   DT_OAUTH_CLIENT_ID=dt0s02.XXXXXXXX
   DT_OAUTH_CLIENT_SECRET=YYYYYYYY
   DT_ACCOUNT_UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
   ```

**How the exchange works** (what `import_dashboard.sh` does for you):

```bash
curl -s -X POST https://sso.dynatrace.com/sso/oauth2/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=client_credentials' \
  --data-urlencode "client_id=$DT_OAUTH_CLIENT_ID" \
  --data-urlencode "client_secret=$DT_OAUTH_CLIENT_SECRET" \
  --data-urlencode 'scope=document:documents:write' \
  --data-urlencode "resource=urn:dtaccount:$DT_ACCOUNT_UUID"
```

The `resource` parameter must be the **`urn:dtaccount:<account-uuid>`** form. An
environment URL there yields a token that 403s on every environment API — a
mistake worth knowing about, because it looks like a permissions problem rather
than a malformed request.

Access tokens from this flow expire in ~300 seconds — ample for an import, so
request one per run rather than caching it.

---

## Which failure means what

| Symptom | Cause |
|---|---|
| OTLP export logs 401 | token not recognised, or `Bearer` used where `Api-Token` is expected |
| OTLP export logs 403 | token is valid but missing `*.ingest` scope |
| OTLP export logs 404 | path mismatch — signal suffix missing, or `/v1/metrics` instead of `/api/v2/otlp/v1/metrics` |
| Metrics API 401 on verify | token lacks `metrics.read` (separate from `metrics.ingest`) |
| Metrics API 404 for a metric | never ingested, or first-ingest registration lag (minutes) |
| Metrics API 200 but zero data points | cumulative temporality — Dynatrace dropped it |
| Dashboard import 401 | ingest `Api-Token` used instead of a `Bearer` platform token |
| Dashboard import 403 | scope is right, the underlying user isn't permitted (or the OAuth `resource` wasn't the account URN) |
| Dashboard import 409 | a dashboard with that name already exists — set `DASHBOARD_NAME` |

## Sources

- [Access tokens (classic) and scope list](https://docs.dynatrace.com/docs/manage/identity-access-management/access-tokens-and-oauth-clients/access-tokens)
- [Platform tokens](https://docs.dynatrace.com/docs/manage/identity-access-management/access-tokens-and-oauth-clients/platform-tokens)
- [OAuth clients](https://docs.dynatrace.com/docs/manage/identity-access-management/access-tokens-and-oauth-clients/oauth-clients)
- [API for Dashboards and Notebooks (document service)](https://docs.dynatrace.com/docs/analyze-explore-automate/dashboards-and-notebooks/document-api)
- [OTLP export endpoints](https://docs.dynatrace.com/docs/ingest-from/opentelemetry/otlp-api)
