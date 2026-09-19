"""Audit batch plugin — async batched audit log forwarding (spec section 6.5)."""
import asyncio, re, time, logging
from typing import Optional
import httpx
from config import settings

logger = logging.getLogger("gateway.audit")

PAT_MASK_PATTERN = re.compile(r'(hdpat_[A-Za-z0-9]{12}_)[A-Za-z0-9_-]{43}')

_batch: list[dict] = []
_batch_lock = asyncio.Lock()
_last_flush = time.time()

def _mask_pat(text: str) -> str:
    if not text:
        return text
    return PAT_MASK_PATTERN.sub(r'\1****', text)

async def record_event(
    event_type: str,
    token_id: Optional[str],
    user_sub: Optional[str],
    jwt_jti: Optional[str],
    trace_id: Optional[str],
    client_ip: Optional[str],
    method: Optional[str],
    path: Optional[str],
    status_code: Optional[int],
    latency_ms: Optional[int],
    error_code: Optional[str] = None,
):
    entry = {
        "event_type": event_type,
        "token_id": token_id,
        "user_sub": user_sub,
        "jwt_jti": jwt_jti,
        "trace_id": trace_id,
        "client_ip": client_ip,
        "http_method": method,
        "request_path": _mask_pat(path),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "error_code": error_code,
    }
    async with _batch_lock:
        _batch.append(entry)
        if len(_batch) >= settings.audit_batch_size or (time.time() - _last_flush) > settings.audit_flush_interval_s:
            await _flush()

async def _flush():
    global _batch, _last_flush
    if not _batch:
        return
    to_send = _batch[:]
    _batch = []
    _last_flush = time.time()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{settings.portal_backend_url}/internal/audit", json=to_send)
    except Exception as e:
        logger.warning(f"Audit flush failed: {e} (dropped {len(to_send)} events)")

async def flush_all():
    """Called on shutdown to flush remaining events."""
    async with _batch_lock:
        await _flush()
