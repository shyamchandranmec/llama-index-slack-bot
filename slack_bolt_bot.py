import os
import json
import logging
import asyncio
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from llama_index.core.memory import Memory
from llama_index.core.base.llms.types import ChatMessage

from main import agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv()

PERSIST_MODE = os.getenv("PERSIST_MODE", "").upper()  # LOCAL, REDIS, or ""
MEMORY_DIR = os.getenv("MEMORY_DIR", ".agent_memory")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

thread_memories: dict[str, Memory] = {}

# ── Redis setup ────────────────────────────────────────────────────────────────
_redis_client = None
if PERSIST_MODE == "REDIS":
    import redis.asyncio as aioredis
    _redis_client = aioredis.from_url(REDIS_URL)
    log.info("Memory persistence: REDIS (%s)", REDIS_URL)
elif PERSIST_MODE == "LOCAL":
    log.info("Memory persistence: LOCAL (%s)", MEMORY_DIR)
else:
    log.info("Memory persistence: IN-MEMORY only")

# ── LOCAL helpers ──────────────────────────────────────────────────────────────
def _local_path(session_id: str) -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"{session_id}.json")


async def _load_local(session_id: str) -> Memory:
    path = _local_path(session_id)
    if os.path.exists(path):
        try:
            with open(path) as f:
                messages = [ChatMessage.model_validate(m) for m in json.load(f)]
            log.info("Loaded %d messages for session %s", len(messages), session_id)
            return Memory.from_defaults(session_id=session_id, chat_history=messages)
        except Exception as e:
            log.warning("Could not load memory for %s (%s), starting fresh", session_id, e)
    return Memory.from_defaults(session_id=session_id)


async def _save_local(session_id: str, memory: Memory) -> None:
    messages = await memory.aget_all()
    data = [m.model_dump(mode="json") for m in messages]
    with open(_local_path(session_id), "w") as f:
        json.dump(data, f)
    log.info("Saved %d messages for session %s", len(data), session_id)


# ── REDIS helpers ──────────────────────────────────────────────────────────────
async def _load_redis(session_id: str) -> Memory:
    try:
        raw = await _redis_client.get(f"memory:{session_id}")
        if raw:
            messages = [ChatMessage.model_validate(m) for m in json.loads(raw)]
            log.info("Loaded %d messages for session %s from Redis", len(messages), session_id)
            return Memory.from_defaults(session_id=session_id, chat_history=messages)
    except Exception as e:
        log.warning("Could not load memory for %s from Redis (%s), starting fresh", session_id, e)
    return Memory.from_defaults(session_id=session_id)


async def _save_redis(session_id: str, memory: Memory) -> None:
    messages = await memory.aget_all()
    data = json.dumps([m.model_dump(mode="json") for m in messages])
    await _redis_client.set(f"memory:{session_id}", data)
    log.info("Saved %d messages for session %s to Redis", len(messages), session_id)


# ── Unified memory factory ─────────────────────────────────────────────────────
async def get_or_create_memory(session_id: str) -> Memory:
    if session_id not in thread_memories:
        if PERSIST_MODE == "LOCAL":
            thread_memories[session_id] = await _load_local(session_id)
        elif PERSIST_MODE == "REDIS":
            thread_memories[session_id] = await _load_redis(session_id)
        else:
            thread_memories[session_id] = Memory.from_defaults(session_id=session_id)
    return thread_memories[session_id]


async def ask_agent(text: str, session_id: str) -> str:
    memory = await get_or_create_memory(session_id)
    response = await agent.run(user_msg=text, memory=memory)
    if PERSIST_MODE == "LOCAL":
        await _save_local(session_id, memory)
    elif PERSIST_MODE == "REDIS":
        await _save_redis(session_id, memory)
    return str(response)


async def reply(say, event):
    """Send agent response, replying in-thread when applicable."""
    thread_ts = event.get("thread_ts") or event.get("ts")
    session_id = event.get("thread_ts") or event.get("channel")
    text = event.get("text", "")
    log.info("Querying agent with: %r (session=%s)", text, session_id)
    answer = await ask_agent(text, session_id)
    await say(text=answer, thread_ts=thread_ts)


# Direct messages
@app.event("message")
async def handle_dm(event, say, logger):
    if event.get("subtype"):
        return
    if event.get("channel_type") == "im":
        logger.info("DM from user=%s", event.get("user"))
        await reply(say, event)


# @mentions in public/private channels (and threaded mentions)
@app.event("app_mention")
async def handle_mention(event, say, logger):
    logger.info("Mention ts=%s thread_ts=%s", event.get("ts"), event.get("thread_ts"))
    await reply(say, event)


async def main():
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Bolt bot starting in Socket Mode...")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
