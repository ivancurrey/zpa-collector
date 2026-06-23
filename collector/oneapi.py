"""Read-only ZPA OneAPI client (stdlib only).

Mirrors the proven client-credentials flow from zpa_export.py:
  token  -> https://<vanity>.zslogin.net/oauth2/v1/token
  config -> https://api.zsapi.net/zpa/mgmtconfig/<version>/admin/customers/<id>
Paged (page/pagesize=500), paced (~0.6s), retries 429/503 honoring retry-after.
The opener is injectable so tests never hit the network.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 500
GET_PAUSE = 0.6          # seconds between GETs (limit: 20 GET / 10s per IP)
_DEFAULT_RETRY = 13


class OneAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _retry_seconds(header_value):
    digits = "".join(c for c in (header_value or "") if c.isdigit())
    return int(digits) if digits else _DEFAULT_RETRY


class OneAPIClient:
    def __init__(self, settings, *, opener=None):
        self._s = settings
        self._opener = opener or urllib.request.build_opener()
        self._token = None
        self._token_url = (
            f"https://{settings.zs_vanity_domain}.zslogin.net/oauth2/v1/token"
        )

    def _base(self, version):
        return (
            f"https://api.zsapi.net/zpa/mgmtconfig/{version}"
            f"/admin/customers/{self._s.zpa_customer_id}"
        )

    def token(self):
        if self._token:
            return self._token
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._s.zs_client_id,
            "client_secret": self._s.zs_client_secret,
            "audience": "https://api.zscaler.com",
        }).encode("utf-8")
        req = urllib.request.Request(
            self._token_url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self._opener.open(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise OneAPIError(
                f"Token request failed ({e.code}): "
                f"{e.read().decode('utf-8', 'replace')}",
                status_code=e.code,
            )
        except urllib.error.URLError as e:
            raise OneAPIError(f"Cannot reach token endpoint {self._token_url}: {e.reason}")
        tok = payload.get("access_token")
        if not tok:
            raise OneAPIError("No access_token in token response.")
        self._token = tok
        return tok

    def _api_get(self, url):
        token = self.token()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        while True:
            try:
                with self._opener.open(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    wait = _retry_seconds(e.headers.get("retry-after")) + 1
                    time.sleep(wait)
                    continue
                raise OneAPIError(
                    f"GET {url} failed ({e.code}): "
                    f"{e.read().decode('utf-8', 'replace')}",
                    status_code=e.code,
                )
            except urllib.error.URLError as e:
                raise OneAPIError(f"GET {url} failed: {e.reason}")

    def paged_get(self, resource, *, microtenant_id=None, version="v1"):
        items, page = [], 1
        base = self._base(version)
        while True:
            params = {"page": page, "pagesize": PAGE_SIZE}
            if microtenant_id is not None:   # NEVER send microtenantId=null (HTTP 400)
                params["microtenantId"] = microtenant_id
            url = f"{base}/{resource}?{urllib.parse.urlencode(params)}"
            data = self._api_get(url)
            items.extend(data.get("list") or [])
            total = int(data.get("totalPages") or 1)
            if page >= total:
                return items
            page += 1
            time.sleep(GET_PAUSE)

    def list_microtenants(self):
        try:
            rows = self.paged_get("microtenants")
        except OneAPIError as e:
            if e.status_code == 404:   # feature endpoint absent on this tenant
                return [None]
            raise
        return [None] + [r["id"] for r in rows if r.get("id")]
