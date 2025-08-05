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
def handle_translate_command(ack, command, logger):
    subcommand = command.get('text', '').strip().lower()
    channel_id = command['channel_id']
    response_text = "Invalid command. Please use `/translate-gem-channel add`, `/translate-gem-channel remove`, or `/translate-gem-channel list`."

    try:
        if subcommand == 'add':
            if channel_manager.add_channel(channel_id):
                response_text = "✅ This channel is now enabled for real-time translation. Please make sure I am invited to this channel."
            else:
                response_text = "This channel is already enabled for translation."
        elif subcommand == 'remove':
            if channel_manager.remove_channel(channel_id):
                response_text = "Real-time translation has been disabled for this channel."
            else:
                response_text = "This channel was not enabled for translation."
        elif subcommand == 'list':
            registered_channels = channel_manager.get_channels()
            if registered_channels:
                channel_links = [f"<#{c}>" for c in registered_channels]
                response_text = f"Real-time translation is currently active in the following channels: {', '.join(channel_links)}"
            else:
                response_text = "Real-time translation is not active in any channels."
        
        ack(text=response_text)

    except Exception as e:
        logger.error(f"Error handling /translate-gem-channel command: {e}")
        ack(text=f"An error occurred while processing your command: {e}")

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

def ask_to_translate_title(say, channel, ts, page_id, original_title, new_title, title_prop_name):
    """Asks the user if they want to translate the Notion document title."""
    say(
        channel=channel,
        thread_ts=ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"In accordance with company policy, Notion document titles should be in English. Would you like to translate the title as follows?\n\n*Current Title:* {original_title}\n*Suggested Title:* {new_title}"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Yes, change it"},
                        "style": "primary",
                        "action_id": "translate_title_confirm",
                        "value": json.dumps({"page_id": page_id, "new_title": new_title, "title_prop_name": title_prop_name})
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "No"},
                        "style": "danger",
                        "action_id": "translate_title_cancel"
                    }
                ]
            }
        ]
    )

@app.action("translate_title_cancel")
def handle_translate_title_cancel(ack, body):
    ack()
    app.client.chat_delete(channel=body['channel']['id'], ts=body['message']['ts'])

@app.action("translate_title_confirm")
def handle_translate_title_confirm(ack, body, say, logger):
    ack()
    action_details = json.loads(body['actions'][0]['value'])
    page_id = action_details['page_id']
    new_title = action_details['new_title']
    title_prop_name = action_details['title_prop_name']
    
    original_ts = body['message']['ts']
    channel_id = body['channel']['id']

    try:
        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":hourglass_flowing_sand: Changing Notion document title to '{new_title}'..."
                    }
                }
            ],
            text=f"Changing Notion document title..."
        )

        notion.pages.update(
            page_id=page_id,
            properties={
                title_prop_name: {
                    "title": [{"text": {"content": new_title}}]
                }
            }
        )

        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[],
            text=f"✅ Notion document title successfully changed to: *{new_title}*"
        )

    except Exception as e:
        logger.error(f"Error updating Notion page title: {e}")
        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[],
            text=f":warning: An error occurred while updating the Notion page title: ```{e}```"
        )


def translate_message(event, say, logger):
    """
    메시지를 번역하고 올바른 위치(채널 또는 스레드)에 게시합니다.
    Notion 링크가 포함된 경우, 번역 메시지 아래 스레드에 제안을 보냅니다.
    """
    channel_id = event.get('channel')
    user_id = event.get('user')
    text = event.get('text')
    
    # 원본 메시지가 스레드에 있는지 확인합니다.
    thread_ts_from_event = event.get('thread_ts')
    
    thinking_response = None
    try:
        # 1. 모든 사용자에게 보이는 "생각 중" 메시지를 보냅니다.
        thinking_messages = [
            "Interpreting Heptapod Language…", "Translating to Mentalese…",
            "Analyzing linguistic patterns…", "Connecting to the universal translator…"
        ]
        thinking_message_text = random.choice(thinking_messages)
        
        thinking_response = say(
            text=f":thought_balloon: {thinking_message_text}",
            thread_ts=thread_ts_from_event
        )

        # 2. URL과 텍스트를 분리합니다.
        url_pattern = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
        urls = re.findall(url_pattern, text)
        text_to_translate = re.sub(url_pattern, "", text).strip()

        # 3. 번역을 수행합니다.
        if text_to_translate:
            prompt = f"You are a translator. Detect the language of the following text. If it is Korean, translate it to English. For all other languages, translate it to Korean. Please format the translation using Slack's markdown syntax for optimal display (e.g., use *bold* instead of **bold**). Do not add any other text to the response, only the translated text itself. Text to translate: {text_to_translate}"
            
            translation_response = model.generate_content(prompt)
            translated_text = translation_response.text.strip()
        else:
            translated_text = ""

        # 4. 번역된 텍스트와 원본 URL을 조합합니다.
        final_translated_text = f"{translated_text} {' '.join(urls)}".strip()

        is_korean = any(c >= '가' and c <= '힣' for c in text)
        if is_korean:
            reply_text = f"🌐 *Translation (EN) from <@{user_id}>:*{final_translated_text}"
        else:
            reply_text = f"🌐 *번역 (KR) from <@{user_id}>:*{final_translated_text}"

        # 5. "생각 중" 메시지를 최종 번역 결과로 업데이트합니다.
        app.client.chat_update(
            channel=channel_id,
            ts=thinking_response['ts'],
            text=reply_text
        )
        
        # 6. Notion 링크를 확인하고 제안/오류 메시지를 보낼 위치를 결정합니다.
        page_id = get_page_id_from_url(text)
        if page_id:
            # 원본이 스레드 댓글이면 해당 스레드에, 아니면 번역 메시지(업데이트된 메시지)에 스레드를 만듭니다.
            thread_for_notion = thread_ts_from_event if thread_ts_from_event else thinking_response['ts']
            
            logger.info(f"Found a Notion Page ID in the translated message: {page_id}")
            try:
                page = notion.pages.retrieve(page_id=page_id)
                
                properties = page.get('properties', {})
                title_property = None
                title_prop_name = None
                for prop_name, prop_details in properties.items():
                    if prop_details.get('type') == 'title':
                        title_property = prop_details
                        title_prop_name = prop_name
                        break
                
                if title_property and title_prop_name:
                    original_title = title_property.get('title', [{}])[0].get('plain_text', 'Untitled')
                    
                    if any('가' <= char <= '힣' for char in original_title):
                        prompt = f"Translate the following Korean document title to English. Respond with only the translated title, without any additional text or quotation marks. Title: '{original_title}'"
                        title_translation_response = model.generate_content(prompt)
                        english_title = title_translation_response.text.strip()
                        new_title_format = f"{original_title} ({english_title})"
                        
                        ask_to_translate_title(say, channel_id, thread_for_notion, page_id, original_title, new_title_format, title_prop_name)
                else:
                    logger.warning(f"Could not find a title property for page ID: {page_id}")

            except notion_client.errors.APIResponseError as e:
                if e.code == "object_not_found":
                    error_message_kr = (
                        ":warning: Notion 페이지에 접근할 수 없습니다. "
                        "페이지가 존재하지 않거나, 저에게 접근 권한이 없는 것 같아요."
                    )
                    error_message_en = (
                        ":warning: I can't access that Notion page. "
                        "It might not exist, or I may not have permission."
                    )
                    say(channel=channel_id, thread_ts=thread_for_notion, text=f"{error_message_kr}\n\n{error_message_en}")
                else:
                    logger.error(f"Notion API Error: {e}")
                    say(channel=channel_id, thread_ts=thread_for_notion, text=f":warning: An error occurred with the Notion API: ```{e}```")
            except Exception as e:
                logger.error(f"An unexpected error occurred while handling Notion link: {e}")
                say(channel=channel_id, thread_ts=thread_for_notion, text=f":warning: An unexpected error occurred while processing the Notion link: ```{e}```")

    except Exception as e:
        logger.error(f"Error during translation process: {e}")
        if thinking_response and thinking_response.get('ts'):
            app.client.chat_update(
                channel=channel_id,
                ts=thinking_response['ts'],
                text=f"Sorry, an error occurred during translation: {e}"
            )
        else:
            say(text=f"Sorry, an error occurred during translation: {e}")




@app.event("message")
def handle_message_events(body, say, logger):
    event = body.get('event', {})
    # --- Robust Bot Message Check ---
    if event.get("bot_id"):
        return
    
    # is_bot_id = event.get("message", {}).get("bot_id") or event.get("message", {}).get("attachments", [{}])[0].get("bot_id")
    if event.get("subtype") == "message_changed": #and is_bot_id:
        logger.info(">>> skip because the message is made by bot")
        return
    # --- End of Check ---

    def process_event(event_data):
        logger.info(f"--- New User Event Received --- \nTEXT: {event_data.get('text')}")
        if should_translate(event_data):
            translate_message(event_data, say, logger)

    # if event.get("subtype") == "message_changed":
    #     message = event.get("message", {})
    #     if message.get("user"):
    #         event_for_translation = {
    #             "channel": event.get("channel"),
    #             "channel_type": event.get("channel_type"),
    #             "user": message.get("user"),
    #             "text": message.get("text"),
    #             "ts": message.get("ts"),
    #             "thread_ts": event.get("thread_ts", message.get("ts")),
    #         }
    #         process_event(event_for_translation)
    # else:
    process_event(event)

if __name__ == "__main__":
    logger.info("Starting bot...")
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()