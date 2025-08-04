import os
import logging
import json
import random
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import vertexai
from vertexai.generative_models import GenerativeModel

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
    
    # Check if the joined user is the bot itself and if it's a group DM
    if user_id == app.client.auth_test()["user_id"] and channel_type == 'mpim':
        try:
            say(
                channel=channel_id,
                text="Hello! I\'ve been invited to this direct message channel and will now automatically translate messages. No extra commands needed!"
            )
            logger.info(f"Joined and sent welcome message to mpim channel: {channel_id}")
        except Exception as e:
            logger.error(f"Failed to send welcome message to {channel_id}: {e}")

def should_translate(event):
    """Determine if a message should be translated."""
    channel_id = event.get('channel')
    channel_type = event.get('channel_type')
    
    # Ignore messages from bots or with no text
    if event.get('bot_id') or not event.get('text'):
        return False
        
    # Translate if it\'s a registered channel, a 1:1 DM, or a group DM
    return channel_manager.is_channel_registered(channel_id) or channel_type in ['im', 'mpim']

def translate_message(event, say, logger):
    """Translate a message and post it."""
    channel_id = event.get('channel')
    channel_type = event.get('channel_type')
    user_id = event.get('user')
    text = event.get('text')
    ts = event.get('ts')
    
    # Determine if the reply should be in a thread
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
        logger.info(f"Posted thinking message at ts: {thinking_response['ts']}")

        # 2. Perform the translation
        prompt = f"You are a translator. Detect the language of the following text. If it is Korean, translate it to English. For all other languages, translate it to Korean. Please format the translation using Slack's markdown syntax for optimal display (e.g., use *bold* instead of **bold**). Do not add any other text to the response, only the translated text itself. Text to translate: {text}"
        
        translation_response = model.generate_content(prompt)
        translated_text = translation_response.text.strip()
        logger.info(f"Successfully translated text: \"{translated_text}\"")

        is_korean = any(c >= '\uac00' and c <= '\ud7a3' for c in text)
        
        # Construct the reply text with mentions only for multi-person contexts
        if is_korean:
            reply_text = f"🌐 *Translation (EN):*\n\n<@{user_id}> {translated_text}"
        else:
            reply_text = f"🌐 *번역 (KR):*\n\n<@{user_id}> {translated_text}"

        # 3. Update the thinking message with the final result
        app.client.chat_update(
            channel=channel_id,
            ts=thinking_response['ts'],
            text=reply_text
        )
        logger.info(f"Successfully updated message at {thinking_response['ts']} with translation.")

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
    # Handle message edits (subtype 'message_changed') within the main message handler
    if event.get("subtype") == "message_changed":
        message = event.get("message", {})
        # To avoid loops, ensure the user who changed the message is not the bot itself
        if message.get("user") != app.client.auth_test()["user_id"]:
             # Re-use the event structure for the translation function
            event_for_translation = {
                "channel": event.get("channel"),
                "channel_type": event.get("channel_type"),
                "user": message.get("user"),
                "text": message.get("text"),
                "ts": message.get("ts"),
                "thread_ts": event.get("thread_ts", message.get("ts"))
            }
            if should_translate(event_for_translation):
                translate_message(event_for_translation, say, logger)
    elif should_translate(event):
        translate_message(event, say, logger)

if __name__ == "__main__":
    logger.info("Starting bot...")
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()