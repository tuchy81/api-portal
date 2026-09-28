"""Zone 1 client — CitizenApiClient for Python automation scripts."""
import os, time, requests

class CitizenApiClient:
    def __init__(self, token: str, gateway_url: str = "http://localhost:9080"):
        if not token.startswith("hdpat_"):
            raise ValueError("Token must be in hdpat_<tokenId>_<secret> format")
        self.token = token
        self.gateway_url = gateway_url
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/json"

    def call(self, method: str, path: str, **kwargs) -> requests.Response:
        resp = self.session.request(method, self.gateway_url + path, timeout=30, **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "30"))
            remaining = resp.headers.get("X-RateLimit-Remaining", "0")
            print(f"[429] Quota exceeded. Remaining: {remaining}. Retry after {retry_after}s")
        return resp

    def list_vendors(self, page: int = 1, size: int = 50) -> requests.Response:
        return self.call("GET", f"/capi/v1/vendors", params={"page": page, "size": size})

    def get_vendor(self, vendor_id: str) -> requests.Response:
        return self.call("GET", f"/capi/v1/vendors/{vendor_id}")

    def create_vendor(self, name: str, code: str) -> requests.Response:
        return self.call("POST", "/capi/v1/vendors", json={"name": name, "code": code})

    def list_orders(self, page: int = 1, size: int = 50) -> requests.Response:
        return self.call("GET", "/capi/v1/orders", params={"page": page, "size": size})

    def list_employees(self) -> requests.Response:
        return self.call("GET", "/capi/v1/employees")


class PortalApiClient:
    """Portal management client — requires SSO JWT."""
    def __init__(self, jwt: str, portal_url: str = "http://localhost:8082"):
        self.jwt = jwt
        self.portal_url = portal_url
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {jwt}"
        self.session.headers["Content-Type"] = "application/json"
        self.base = portal_url + "/portal/v1"

    def get_me(self):
        return self.session.get(f"{self.base}/me").json()

    def list_catalog(self, q: str = None):
        return self.session.get(f"{self.base}/catalog/apis", params={"q": q}).json()

    def submit_application(self, api_id: str, purpose: str, valid_until: str = "2027-03-31"):
        return self.session.post(f"{self.base}/applications", json={
            "apiId": api_id,
            "purpose": purpose,
            "expectedTps": 5,
            "expectedDaily": 2000,
            "validUntil": valid_until,
            "requestedScopes": [],
        }).json()

    def approve_application(self, app_id: str, scopes: list):
        return self.session.patch(f"{self.base}/applications/{app_id}",
                                  json={"action": "APPROVE", "grantedScopes": scopes}).json()

    def issue_pat(self, app_id: str, name: str, valid_days: int = 90):
        resp = self.session.post(f"{self.base}/tokens",
                                 json={"appId": app_id, "tokenName": name, "validDays": valid_days})
        return resp.json()

    def revoke_pat(self, token_id: str):
        return self.session.delete(f"{self.base}/tokens/{token_id}").status_code


if __name__ == "__main__":
    # Quick smoke test — get token from env
    pat = os.environ.get("CAPI_TOKEN", "")
    if pat:
        client = CitizenApiClient(pat)
        resp = client.list_vendors()
        print(f"Status: {resp.status_code}")
        print(f"Remaining: {resp.headers.get('X-RateLimit-Remaining', 'N/A')}")
        if resp.status_code == 200:
            print(resp.json())
