"""PAT generation and cryptographic operations."""
import secrets, hmac, hashlib, base64
from argon2 import PasswordHasher

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Argon2id: OWASP recommended minimum (m=19MiB, t=2, p=1)
PH = PasswordHasher(
    memory_cost=19456,  # 19 MiB in KiB
    time_cost=2,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

def generate_token_id() -> str:
    """Generate a base62 12-character token ID from CSPRNG."""
    value = int.from_bytes(secrets.token_bytes(9), 'big')
    result = []
    for _ in range(12):
        result.append(BASE62[value % 62])
        value //= 62
    return ''.join(reversed(result))

def generate_secret() -> str:
    """Generate 32-byte CSPRNG secret → base64url 43 chars (no padding)."""
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

def build_pat(token_id: str, secret: str) -> str:
    """Assemble the full PAT string."""
    return f"hdpat_{token_id}_{secret}"

def parse_pat(pat: str) -> tuple[str, str]:
    """Parse hdpat_<tokenId>_<secret> → (tokenId, secret). Raises ValueError on bad format."""
    if not pat.startswith("hdpat_"):
        raise ValueError("Invalid PAT prefix")
    parts = pat.split("_", 2)
    if len(parts) != 3 or len(parts[1]) != 12 or len(parts[2]) != 43:
        raise ValueError("Invalid PAT format")
    return parts[1], parts[2]

def hash_secret_argon2(secret: str) -> str:
    """Argon2id hash for DB storage."""
    return PH.hash(secret)

def verify_argon2(secret: str, stored_hash: str) -> bool:
    """Verify secret against stored Argon2id hash."""
    try:
        return PH.verify(stored_hash, secret)
    except Exception:
        return False

def compute_hmac(secret: str, server_key: str) -> str:
    """HMAC-SHA256(secret, server_key) → hex. Stored in Redis for fast gateway comparison."""
    return hmac.new(server_key.encode(), secret.encode(), hashlib.sha256).hexdigest()

def verify_hmac(secret: str, server_key: str, expected_hmac: str) -> bool:
    """Constant-time HMAC comparison."""
    actual = compute_hmac(secret, server_key)
    return hmac.compare_digest(actual, expected_hmac)

def mask_pat(text: str) -> str:
    """Replace PAT secret portion with **** for logging."""
    import re
    return re.sub(r'(hdpat_[A-Za-z0-9]{12}_)[A-Za-z0-9_-]{43}', r'\1****', text)
