import os
import logging
import json
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

@app.command("/translate-gem")
def handle_translate_command(ack, command, say):
    ack()
    # The command text is expected to be in the format "add", "remove", or "list"
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
            channel_links = [f"<#{c}>" for c in registered_channels]
            say(f"Real-time translation is currently active in the following channels: {', '.join(channel_links)}")
        else:
            say("Real-time translation is not active in any channels.")
    else:
        say("Invalid command. Please use `/translate-gem add`, `/translate-gem remove`, or `/translate-gem list`.")

# TODO: 메시지 이벤트 핸들러 구현

@app.event("message")
def handle_message_events(body, logger):
    event = body.get('event', {})
    channel_id = event.get('channel')
    channel_type = event.get('channel_type') # 'im' for DMs
    user_id = event.get('user')
    text = event.get('text')
    ts = event.get('ts')

    # 봇 자신의 메시지는 무시
    if event.get('bot_id'):
        return

    # 등록된 채널이거나 DM이 아니면 무시
    if not (channel_manager.is_channel_registered(channel_id) or channel_type == 'im'):
        return

    # 텍스트가 없는 메시지는 무시
    if not text:
        return

    try:
        # Gemini를 사용하여 번역 수행
        prompt = f"You are a translator. First, detect if the following text is Korean or English. If it is Korean, translate it to English. If it is English, translate it to Korean. Do not add any other text to the response, only the translated text itself. Text to translate: {text}"
        response = model.generate_content(prompt)
        translated_text = response.text.strip()

        # 원본 언어 감지 (간단한 방법)
        is_korean = any(c >= '\uac00' and c <= '\ud7a3' for c in text)
        
        if is_korean:
            reply_text = f"🌐 **Translation (EN):**\n\n{translated_text}"
        else:
            reply_text = f"🌐 **번역 (KR):**\n\n{translated_text}"

        # 채널에서는 스레드에, DM에서는 일반 메시지로 응답
        if channel_type == 'im':
            app.client.chat_postMessage(channel=channel_id, text=reply_text)
        else:
            app.client.chat_postMessage(channel=channel_id, text=reply_text, thread_ts=ts)

    except Exception as e:
        logger.error(f"Error during translation: {e}")


# 앱 시작

if __name__ == "__main__":
    logger.info("Starting bot...")
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()