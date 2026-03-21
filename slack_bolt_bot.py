import os
import re
import json
import logging
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
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
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", ".uploads"))

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


async def _persist_memory(session_id: str, memory: Memory) -> None:
    if PERSIST_MODE == "LOCAL":
        await _save_local(session_id, memory)
    elif PERSIST_MODE == "REDIS":
        await _save_redis(session_id, memory)


async def ask_agent(text: str, session_id: str) -> str:
    memory = await get_or_create_memory(session_id)
    response = await agent.run(user_msg=text, memory=memory)
    await _persist_memory(session_id, memory)
    return str(response)


# ── File upload helpers ────────────────────────────────────────────────────────
def _unique_path(folder: Path, name: str) -> Path:
    """Return a non-colliding path, appending _1, _2, ... before extension if needed."""
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        dest = folder / f"{stem}_{i}{suffix}"
        if not dest.exists():
            return dest
        i += 1


def _register_file(session_id: str, file_info: dict, saved_path: Path) -> None:
    """Append file metadata to the session's files.json registry."""
    registry = UPLOADS_DIR / session_id / "files.json"
    entries = json.loads(registry.read_text()) if registry.exists() else []
    entries.append({
        "original_name": file_info.get("name"),
        "saved_path": str(saved_path),
        "mimetype": file_info.get("mimetype"),
        "ts": file_info.get("timestamp") or file_info.get("created"),
    })
    registry.write_text(json.dumps(entries, indent=2))


async def download_slack_file(file_info: dict, session_id: str) -> Path:
    """Download a Slack file, save under .uploads/{session_id}/, deduplicate if needed,
    and register in files.json."""
    url = file_info.get("url_private_download") or file_info.get("url_private")
    name = file_info.get("name", "file")
    folder = UPLOADS_DIR / session_id
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(folder, name)
    headers = {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}
    async with aiohttp.ClientSession() as http:
        async with http.get(url, headers=headers) as resp:
            dest.write_bytes(await resp.read())
    _register_file(session_id, file_info, dest)
    log.info("Saved file %s (session=%s)", dest, session_id)
    return dest


async def process_and_reply(channel: str, thread_ts: str, session_id: str, file_infos: list[dict], text: str) -> None:
    """Background task: download all files, proc e ss them together, then post one result to the thread."""
    log.info("Starting file processing for session %s: %d file(s)", session_id, len(file_infos))    
    file_paths = await asyncio.gather(*[download_slack_file(fi, session_id) for fi in file_infos])

    memory = await get_or_create_memory(session_id)
    for fi, fp in zip(file_infos, file_paths):
        log.info("Adding file to memory for session %s: %s at %s", session_id, fi.get("name", "unknown"), fp)       
        await memory.aput(ChatMessage(
            role="system",
            content=f"[File uploaded] name={fi.get('name', 'unknown')}, path={fp}",
        ))
    await _persist_memory(session_id, memory)

    file_parts = ", ".join(
        f"'{fi.get('name', 'unknown')}' at '{fp}'"
        for fi, fp in zip(file_infos, file_paths)
    )
    log.info("All files for session %s saved: %s", session_id, file_parts)
    prompt = f"The user uploaded the following file(s): {file_parts}. {text}"
    log.info("Processing %d file(s) for session %s, question: %s", len(file_infos), session_id, text)

    result = str(await ask_agent(prompt, session_id))
    log.info("Agent response for session %s: %s", session_id, result)
    await app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=result)


async def reply(say, event):
    """Send agent response, replying in-thread when applicable."""
    session_id = event.get("thread_ts") or event.get("channel")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    raw_text = event.get("text") or ""
    # Strip bot mention tokens so we can detect if a real question was asked
    text = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()
    files = event.get("files", [])

    if files:
        if not text:
            # File with no question — prompt the user
            await say(
                text="I received your file! What would you like to know about it?",
                thread_ts=thread_ts,
            )
            return

        # File(s) + question — ack immediately, process all together in background
        await say(
            text="Got it! I'll take a look at your file(s) and get back to you shortly.",
            thread_ts=thread_ts,
        )
        asyncio.create_task(process_and_reply(channel, thread_ts, session_id, files, text))
    else:
        # Text only — process directly
        if text:
            log.info("Querying agent with: %r (session=%s)", text, session_id)
            answer = await ask_agent(text, session_id)
            await say(text=answer, thread_ts=thread_ts)
        else:
            log.info("No text to process for session %s", session_id)
            await say(text="I received your message but couldn't find any text to process. Please ask a question or upload a file!", thread_ts=thread_ts)


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
