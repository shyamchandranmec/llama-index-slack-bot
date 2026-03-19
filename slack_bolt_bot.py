import os
import logging
import asyncio
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from llama_index.core.memory import Memory

from main import agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv()

app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

thread_memories: dict[str, Memory] = {}


def get_or_create_memory(session_id: str) -> Memory:
    if session_id not in thread_memories:
        thread_memories[session_id] = Memory.from_defaults(session_id=session_id)
    return thread_memories[session_id]


async def ask_agent(text: str, session_id: str) -> str:
    memory = get_or_create_memory(session_id)
    response = await agent.run(user_msg=text, memory=memory)
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
