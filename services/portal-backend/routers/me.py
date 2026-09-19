"""Current user profile endpoint."""
from fastapi import APIRouter, Depends
from auth import get_current_user, UserClaims

router = APIRouter(prefix="/me", tags=["auth"])

@router.get("")
async def get_me(user: UserClaims = Depends(get_current_user)):
    return {
        "sub": user.sub,
        "username": user.username,
        "roles": user.roles,
        "isAdmin": "platform-admin" in user.roles,
        "isApiOwner": "api-owner" in user.roles,
        "isAuditor": "auditor" in user.roles,
    }
