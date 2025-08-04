import os
import logging
import json
import random
import re
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import vertexai
from vertexai.generative_models import GenerativeModel
import notion_client

# .env 파일 (봇 특정 및 공통) 로드
load_dotenv() # 현재 디렉터리의 .env 로드
load_dotenv(dotenv_path="../.env") # 상위 디렉터리의 .env 로드

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Slack 앱 초기화
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# Vertex AI 초기화
vertexai.init(project=os.environ.get("GOOGLE_CLOUD_PROJECT"), location=os.environ.get("GOOGLE_CLOUD_LOCATION"))
model = GenerativeModel("gemini-2.5-flash")

# Notion 클라이언트 초기화
notion = notion_client.Client(auth=os.environ.get("NOTION_API_KEY"))


class ChannelManager:
    def __init__(self, file_path='registered_channels.json'):
        self.file_path = file_path
        self.channels = self._load_channels()

    def _load_channels(self):
        try:
            with open(self.file_path, 'r') as f:
                return set(json.load(f))
        except FileNotFoundError:
            return set()

    def _save_channels(self):
        with open(self.file_path, 'w') as f:
            json.dump(list(self.channels), f)

    def add_channel(self, channel_id):
        if channel_id not in self.channels:
            self.channels.add(channel_id)
            self._save_channels()
            return True
        return False

    def remove_channel(self, channel_id):
        if channel_id in self.channels:
            self.channels.remove(channel_id)
            self._save_channels()
            return True
        return False

    def get_channels(self):
        return list(self.channels)

    def is_channel_registered(self, channel_id):
        return channel_id in self.channels

channel_manager = ChannelManager()

@app.command("/translate-gem-channel")
def handle_translate_command(ack, command, say, logger):
    ack()
    try:
        subcommand = command.get('text', '').strip().lower()
        channel_id = command['channel_id']

        if subcommand == 'add':
            if channel_manager.add_channel(channel_id):
                say(text="This channel is now enabled for real-time translation.", channel=channel_id)
            else:
                say(text="This channel is already enabled for translation.", channel=channel_id)
        elif subcommand == 'remove':
            if channel_manager.remove_channel(channel_id):
                say(text="Real-time translation has been disabled for this channel.", channel=channel_id)
            else:
                say(text="This channel was not enabled for translation.", channel=channel_id)
        elif subcommand == 'list':
            registered_channels = channel_manager.get_channels()
            if registered_channels:
                channel_links = [f"<#{c}>"]
                say(f"Real-time translation is currently active in the following channels: {', '.join(channel_links)}")
            else:
                say("Real-time translation is not active in any channels.")
        else:
            say("Invalid command. Please use `/translate-gem-channel add`, `/translate-gem-channel remove`, or `/translate-gem-channel list`.")
    except Exception as e:
        logger.error(f"Error handling /translate-gem-channel command: {e}")
        say(f"An error occurred while processing your command: {e}")

@app.event("member_joined_channel")
def handle_member_joined_channel(event, say, logger):
    channel_id = event.get('channel')
    channel_type = event.get('channel_type')
    user_id = event.get('user')
    
    if user_id == app.client.auth_test()["user_id"] and channel_type == 'mpim':
        try:
            say(
                channel=channel_id,
                text="Hello! I've been invited to this direct message channel and will now automatically translate messages. No extra commands needed!"
            )
            logger.info(f"Joined and sent welcome message to mpim channel: {channel_id}")
        except Exception as e:
            logger.error(f"Failed to send welcome message to {channel_id}: {e}")

def should_translate(event):
    """Determine if a message should be translated."""
    if not event.get('text'):
        return False
    channel_id = event.get('channel')
    channel_type = event.get('channel_type')
    return channel_manager.is_channel_registered(channel_id) or channel_type in ['im', 'mpim']

def get_page_id_from_url(url):
    """Extract Notion page ID from a URL."""
    match = re.search(r'[a-f0-9]{32}', url)
    if match:
        return match.group(0)
    return None

def ask_to_translate_document(say, channel, ts, page_id, title):
    """Asks the user if they want to translate the Notion document."""
    is_korean = any(c >= '가' and c <= '힣' for c in title)
    suffix = "_EN" if is_korean else "_KR"
    new_title = f"{title}{suffix}"

    say(
        channel=channel,
        thread_ts=ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Do you want to translate this document and create a new one titled *{new_title}*?"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Yes, create it"},
                        "style": "primary",
                        "action_id": "translate_notion_confirm",
                        "value": json.dumps({"page_id": page_id, "new_title": new_title})
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "No"},
                        "style": "danger",
                        "action_id": "translate_notion_cancel"
                    }
                ]
            }
        ]
    )

@app.action("translate_notion_cancel")
def handle_translate_notion_cancel(ack, body):
    ack()
    app.client.chat_delete(channel=body['channel']['id'], ts=body['message']['ts'])

@app.action("translate_notion_confirm")
def handle_translate_notion_confirm(ack, body, say):
    ack()
    action_details = json.loads(body['actions'][0]['value'])
    page_id = action_details['page_id']
    new_title = action_details['new_title']
    
    app.client.chat_update(
        channel=body['channel']['id'],
        ts=body['message']['ts'],
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":hourglass_flowing_sand: Notion 문서(*{new_title}*) 번역을 시작합니다. 잠시만 기다려주세요..."
                }
            }
        ]
    )
    # TODO: Implement the actual document translation logic here

def translate_message(event, say, logger):
    """Translate a message and post it. If it contains a Notion link, ask to translate the document in a thread."""
    channel_id = event.get('channel')
    channel_type = event.get('channel_type')
    user_id = event.get('user')
    text = event.get('text')
    ts = event.get('ts')
    
    in_thread = channel_type not in ['im', 'mpim']
    thread_ts = ts if in_thread else None

    thinking_response = None
    try:
        # 1. Post a public "thinking" message
        thinking_messages = [
            "Interpreting Heptapod Language…", "Translating to Mentalese…",
            "Analyzing linguistic patterns…", "Connecting to the universal translator…"
        ]
        thinking_message_text = random.choice(thinking_messages)
        
        thinking_response = say(
            text=f":thought_balloon: {thinking_message_text}",
            thread_ts=thread_ts
        )

        # 2. Perform the translation
        prompt = f"You are a translator. Detect the language of the following text. If it is Korean, translate it to English. For all other languages, translate it to Korean. Please format the translation using Slack's markdown syntax for optimal display (e.g., use *bold* instead of **bold**). Do not add any other text to the response, only the translated text itself. Text to translate: {text}"
        
        translation_response = model.generate_content(prompt)
        translated_text = translation_response.text.strip()

        is_korean = any(c >= '가' and c <= '힣' for c in text)
        if is_korean:
            reply_text = f"🌐 *Translation (EN):*\n<@{user_id}> {translated_text}"
        else:
            reply_text = f"🌐 *번역 (KR):*\n<@{user_id}> {translated_text}"

        # 3. Update the thinking message with the final result
        app.client.chat_update(
            channel=channel_id,
            ts=thinking_response['ts'],
            text=reply_text
        )
        
        # 4. NEW: Check for Notion link and ask to translate the document
        page_id = get_page_id_from_url(text)
        if page_id:
            logger.info(f"Found a Notion Page ID in the translated message: {page_id}")
            try:
                page = notion.pages.retrieve(page_id=page_id)
                title_property = page.get('properties', {}).get('title', {})
                original_title = title_property.get('title', [{}])[0].get('plain_text', 'Untitled')
                
                # Ask to translate the document in the original message's thread
                ask_to_translate_document(say, channel_id, ts, page_id, original_title)
            except Exception as e:
                logger.error(f"Error handling Notion link after translation: {e}")
                say(channel=channel_id, thread_ts=ts, text=f":warning: Notion 링크 처리 중 오류가 발생했습니다:```{e}```")

    except Exception as e:
        logger.error(f"Error during translation process: {e}")
        if thinking_response and thinking_response.get('ts'):
            app.client.chat_update(
                channel=channel_id,
                ts=thinking_response['ts'],
                text=f"Sorry, an error occurred during translation: {e}"
            )

@app.event("message")
def handle_message_events(body, say, logger):
    event = body.get('event', {})

    logger.info(f"\n\nhandle_message_events: \n {event}\n")
    # --- Robust Bot Message Check ---
    if event.get("bot_id"):
        return
    
    is_bot_id = event.get("message", {}).get("bot_id") or event.get("message", {}).get("attachments", [{}])[0].get("bot_id")
    if event.get("subtype") == "message_changed" and is_bot_id:
        logger.info(">>> skip because the message is made by bot")
        return
    # --- End of Check ---

    def process_event(event_data):
        logger.info(f"--- New User Event Received --- \nTEXT: {event_data.get('text')}")
        if should_translate(event_data):
            translate_message(event_data, say, logger)

    if event.get("subtype") == "message_changed":
        message = event.get("message", {})
        if message.get("user"):
            event_for_translation = {
                "channel": event.get("channel"),
                "channel_type": event.get("channel_type"),
                "user": message.get("user"),
                "text": message.get("text"),
                "ts": message.get("ts"),
                "thread_ts": event.get("thread_ts", message.get("ts")),
            }
            process_event(event_for_translation)
    else:
        process_event(event)

if __name__ == "__main__":
    logger.info("Starting bot...")
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()