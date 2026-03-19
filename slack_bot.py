import os
import json
import logging
import threading
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.request import SocketModeRequest

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv()

STATIC_RESPONSE = "Hello! I received your message. This is a static response."

web_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
bot_user_id = web_client.auth_test()["user_id"]


def handle_events(client: SocketModeClient, req: SocketModeRequest):
    log.debug("Incoming request type=%s payload=%s", req.type, json.dumps(req.payload))

    if req.type != "events_api":
        return

    # Acknowledge the event immediately
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    event = req.payload.get("event", {})
    event_type = event.get("type")
    log.info("Event type=%s subtype=%s", event_type, event.get("subtype"))

    # Only handle message events (no subtypes = new messages, not edits/joins/etc.)
    if event_type != "message" or event.get("subtype"):
        return

    channel = event.get("channel", "")
    text = event.get("text", "")
    user = event.get("user")

    log.info("Message from user=%s channel=%s text=%r", user, channel, text)

    # Ignore messages from the bot itself
    if user == bot_user_id:
        return

    # DM channels have IDs starting with "D"; channel_type field is not always present
    is_dm = channel.startswith("D")
    bot_mentioned = f"<@{bot_user_id}>" in text

    log.info("is_dm=%s bot_mentioned=%s", is_dm, bot_mentioned)

    # Respond to: DMs, or channel messages that @mention the bot
    if is_dm or bot_mentioned:
        log.info("Sending response to channel=%s", channel)

        def send_response():
            try:
                resp = web_client.chat_postMessage(channel=channel, text=STATIC_RESPONSE)
                log.info("chat_postMessage ok=%s ts=%s", resp["ok"], resp.get("ts"))
            except Exception as e:
                log.error("chat_postMessage failed: %s", e)

        threading.Thread(target=send_response, daemon=True).start()


def main():
    socket_client = SocketModeClient(
        app_token=os.environ["SLACK_APP_TOKEN"],
        web_client=web_client,
    )
    socket_client.socket_mode_request_listeners.append(handle_events)
    socket_client.connect()

    print(f"Bot connected as @{bot_user_id}. Listening for messages...")
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down.")
        socket_client.close()


if __name__ == "__main__":
    main()
