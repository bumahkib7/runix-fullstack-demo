"""Runix Full-Stack Demo — FastAPI + PostgreSQL + Kafka proof-of-concept."""

import os
import asyncio
import json
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ─── Config ──────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
KAFKA_BROKER = os.environ.get("KAFKA_BROKER_URL", "")
PORT = int(os.environ.get("PORT", 8080))


# ─── DB helpers ──────────────────────────────────────────────
pool: asyncpg.Pool | None = None


async def init_db():
    global pool
    if not DATABASE_URL:
        return
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                payload JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)


# ─── Kafka helpers ───────────────────────────────────────────
kafka_producer = None


async def init_kafka():
    global kafka_producer
    if not KAFKA_BROKER:
        return
    try:
        from aiokafka import AIOKafkaProducer
        # Strip kafka:// prefix if present
        broker = KAFKA_BROKER.replace("kafka://", "")
        kafka_producer = AIOKafkaProducer(bootstrap_servers=broker)
        await kafka_producer.start()
    except Exception as e:
        print(f"Kafka init failed (non-fatal): {e}")


# ─── App lifecycle ───────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_kafka()
    yield
    if kafka_producer:
        await kafka_producer.stop()
    if pool:
        await pool.close()


app = FastAPI(title="Runix Full-Stack Demo", version="1.0.0", lifespan=lifespan)


# ─── Models ──────────────────────────────────────────────────
class EventCreate(BaseModel):
    name: str
    payload: dict = {}


class EventOut(BaseModel):
    id: int
    name: str
    payload: dict
    created_at: str


# ─── Routes ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "service": "runix-fullstack-demo",
        "status": "running",
        "database": "connected" if pool else "not configured",
        "kafka": "connected" if kafka_producer else "not configured",
    }


@app.get("/health")
async def health():
    checks = {"api": True}

    if pool:
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["database"] = True
        except Exception:
            checks["database"] = False

    if kafka_producer:
        checks["kafka"] = kafka_producer._sender is not None

    all_ok = all(checks.values())
    return {"healthy": all_ok, "checks": checks}


@app.post("/events", response_model=EventOut)
async def create_event(event: EventCreate):
    if not pool:
        raise HTTPException(status_code=503, detail="Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events (name, payload) VALUES ($1, $2) RETURNING *",
            event.name,
            json.dumps(event.payload),
        )

    # Publish to Kafka if connected
    if kafka_producer:
        try:
            msg = json.dumps({"event": event.name, "payload": event.payload}).encode()
            await kafka_producer.send_and_wait("runix-events", msg)
        except Exception as e:
            print(f"Kafka publish failed (non-fatal): {e}")

    return EventOut(
        id=row["id"],
        name=row["name"],
        payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
        created_at=row["created_at"].isoformat(),
    )


@app.get("/events", response_model=list[EventOut])
async def list_events(limit: int = 20):
    if not pool:
        raise HTTPException(status_code=503, detail="Database not available")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM events ORDER BY id DESC LIMIT $1", limit
        )

    return [
        EventOut(
            id=r["id"],
            name=r["name"],
            payload=json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@app.get("/info")
async def info():
    """Show infrastructure configuration (redacted)."""
    db_url = DATABASE_URL
    if db_url:
        # Redact password
        parts = db_url.split("@")
        db_url = f"***@{parts[-1]}" if len(parts) > 1 else "configured"

    return {
        "database_url": db_url or "not set",
        "kafka_broker": KAFKA_BROKER or "not set",
        "port": PORT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
