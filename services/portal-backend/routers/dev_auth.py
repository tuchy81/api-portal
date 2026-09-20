"""Dev/mock login — issues a real JWT via mock-keycloak token exchange."""
import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from config import settings

router = APIRouter(prefix="/auth", tags=["dev-auth"])

MOCK_USERS = {
    "u-test-001": {"username": "hong.gildong", "roles": ["citizen-developer", "mdm-reader"]},
    "u-test-002": {"username": "api.owner", "roles": ["api-owner", "citizen-developer"]},
    "u-admin-001": {"username": "platform.admin", "roles": ["platform-admin", "citizen-developer"]},
}


@router.get("/dev-users")
def list_dev_users():
    return {
        "users": [
            {"id": uid, "username": u["username"], "roles": u["roles"]}
            for uid, u in MOCK_USERS.items()
        ]
    }


@router.post("/dev-login")
async def dev_login(body: dict):
    user_id = body.get("userId")
    if user_id not in MOCK_USERS:
        return JSONResponse({"error": f"Unknown userId: {user_id}"}, status_code=400)

    token_url = f"{settings.kc_url}/realms/{settings.kc_realm}/protocol/openid-connect/token"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": settings.kc_client_id,
                "client_secret": settings.kc_client_secret,
                "requested_subject": user_id,
                "scope": "openid profile",
            },
        )

    if resp.status_code != 200:
        return JSONResponse(
            {"error": "Token exchange failed", "detail": resp.text},
            status_code=502,
        )

    user = MOCK_USERS[user_id]
    return {
        "token": resp.json()["access_token"],
        "userId": user_id,
        "username": user["username"],
        "roles": user["roles"],
    }
