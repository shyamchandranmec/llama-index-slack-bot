import os
import logging
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv()

STATIC_RESPONSE = "Hello! I received your message. This is a static response."

app = App(token=os.environ["SLACK_BOT_TOKEN"])


def reply(say, event):
    """Reply in-thread if the message is part of a thread, otherwise reply normally."""
    thread_ts = event.get("thread_ts") or event.get("ts")
    say(text=STATIC_RESPONSE, thread_ts=thread_ts)


# Direct messages
@app.event("message")
def handle_dm(event, say, logger):
    channel_type = event.get("channel_type")
    subtype = event.get("subtype")

    if subtype:
        return  # ignore edits, joins, etc.

    logger.info("message event channel_type=%s", channel_type)

    if channel_type == "im":
        reply(say, event)


# @mentions in public/private channels (including thread replies that mention the bot)
@app.event("app_mention")
def handle_mention(event, say, logger):
    logger.info("app_mention event ts=%s thread_ts=%s", event.get("ts"), event.get("thread_ts"))
    reply(say, event)


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Bolt bot starting in Socket Mode...")
    handler.start()
