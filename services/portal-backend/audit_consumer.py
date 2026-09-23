"""Redis Stream consumer for durable security audit events.

Companion to services/apisix-gateway/plugins/apisix/plugins/pat-audit.lua's
XADD path (§9 of the remediation plan). Security-classified events
(AUTH_FAILED / SCOPE_DENIED / QUOTA_EXCEEDED / EXCHANGE_FAILED) are queued
to Redis Stream `cdp:audit:security`; this consumer XREADGROUPs them,
persists to cdp.audit_log, and XACKs. Failure between INSERT and XACK
results in a redelivery on the next poll (idempotent isn't required —
audit_log is append-only; a rare duplicate is preferable to a silent drop).

Runs as an asyncio background task from main.py. Uses `redis.asyncio` so a
long BLOCK on XREADGROUP does not tie up the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as redis_async

from config import settings
from database import get_pool
import pat_utils

logger = logging.getLogger("portal.audit_consumer")

STREAM_KEY = "cdp:audit:security"
GROUP_NAME = "portal-audit"
CONSUMER_NAME = "portal-audit-1"
BLOCK_MS = 5000
BATCH_SIZE = 100


async def _ensure_group(client: redis_async.Redis) -> None:
    """Idempotent XGROUP CREATE. `MKSTREAM` handles the cold-start case where
    no gateway has XADDed yet — otherwise the group create would 404 on the
    key and the consumer would crash-loop until the first security event."""
    try:
        await client.xgroup_create(STREAM_KEY, GROUP_NAME, id="$", mkstream=True)
        logger.info(f"audit stream group created: {GROUP_NAME}@{STREAM_KEY}")
    except redis_async.ResponseError as e:
        if "BUSYGROUP" in str(e):
            return
        raise


async def _persist(entries: list[tuple[str, dict]]) -> list[str]:
    """Insert one batch. Returns the ids we successfully wrote so the caller
    can XACK exactly that subset — anything we couldn't parse or store stays
    unacknowledged and will redeliver on the next XREADGROUP."""
    persisted: list[str] = []
    if not entries:
        return persisted

    pool = await get_pool()
    async with pool.acquire() as db:
        for msg_id, fields in entries:
            raw = fields.get("payload")
            if raw is None:
                logger.warning(f"audit event {msg_id} missing payload field")
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning(f"audit event {msg_id} not JSON: {e}")
                continue

            safe_path = pat_utils.mask_pat(event.get("request_path") or "")
            try:
                await db.execute(
                    """INSERT INTO cdp.audit_log
                       (event_type, token_id, user_sub, jwt_jti, trace_id, client_ip,
                        http_method, request_path, status_code, latency_ms, error_code)
                       VALUES ($1,$2,$3,$4,$5,$6::inet,$7,$8,$9,$10,$11)""",
                    event.get("event_type"), event.get("token_id"),
                    event.get("user_sub"), event.get("jwt_jti"),
                    event.get("trace_id"), event.get("client_ip"),
                    event.get("http_method"), safe_path,
                    event.get("status_code"), event.get("latency_ms"),
                    event.get("error_code"),
                )
                persisted.append(msg_id)
            except Exception as e:
                # Leave unacked — will redeliver. Log so operators can spot
                # a poison message before pending count balloons.
                logger.error(f"audit event {msg_id} insert failed (will redeliver): {e}")
    return persisted


async def _read_and_persist(client: redis_async.Redis) -> None:
    resp = await client.xreadgroup(
        GROUP_NAME, CONSUMER_NAME, streams={STREAM_KEY: ">"},
        count=BATCH_SIZE, block=BLOCK_MS,
    )
    if not resp:
        return
    _, messages = resp[0]  # single stream
    to_persist: list[tuple[str, dict]] = [(msg_id, fields) for msg_id, fields in messages]
    persisted = await _persist(to_persist)
    if persisted:
        await client.xack(STREAM_KEY, GROUP_NAME, *persisted)
        logger.info(f"audit consumer XACKed {len(persisted)}/{len(to_persist)} events")


async def run_consumer() -> None:
    client = redis_async.Redis(
        host=settings.redis_host, port=settings.redis_port,
        password=settings.redis_password, decode_responses=True,
    )
    try:
        await _ensure_group(client)
    except Exception as e:
        logger.error(f"audit consumer XGROUP CREATE failed, will retry: {e}")

    while True:
        try:
            await _read_and_persist(client)
        except asyncio.CancelledError:
            logger.info("audit consumer cancelled")
            break
        except Exception as e:
            # Blanket catch keeps the loop alive across transient Redis or DB
            # errors — the alternative (die and rely on process restart) would
            # bounce the whole portal on a hiccup.
            logger.error(f"audit consumer iteration failed: {e}")
            await asyncio.sleep(2)
    try:
        await client.aclose()
    except Exception:
        pass
