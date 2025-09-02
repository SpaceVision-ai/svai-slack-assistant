import os
import logging
import json
import random
import re
import time
from urllib.parse import urljoin
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import vertexai
from vertexai.generative_models import GenerativeModel, Part, GenerationConfig
import notion_client
import requests
from bs4 import BeautifulSoup

# .env 파일 (봇 특정 및 공통) 로드
load_dotenv() # 현재 디렉터리의 .env 로드
load_dotenv(dotenv_path="../.env") # 상위 디렉터리의 .env 로드

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Slack 앱 초기화
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# 봇 자신의 ID 조회
try:
    BOT_ID = app.client.auth_test()["bot_id"]
    logger.info(f"Initialized with BOT_ID: {BOT_ID}")
except Exception as e:
    logger.error(f"Error getting bot ID: {e}")
    BOT_ID = None

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

class BotThreadMapper:
    def __init__(self, file_path='bot_threads.json'):
        self.file_path = file_path
        self.mappings = self._load_mappings()

    def _load_mappings(self):
        try:
            with open(self.file_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_mappings(self):
        with open(self.file_path, 'w') as f:
            json.dump(self.mappings, f, indent=4)

    def add_mapping(self, original_ts, translated_ts):
        self.mappings[original_ts] = translated_ts
        self._save_mappings()

    def get_translated_thread_ts(self, original_ts):
        return self.mappings.get(original_ts)

bot_thread_mapper = BotThreadMapper()


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
    text = event.get('text', '')
    return bool(text)


def get_page_id_from_url(url):
    """Extract Notion page ID from a URL."""
    match = re.search(r'[a-f0-9]{32}', url)
    if match:
        return match.group(0)
    return None

def fetch_slack_permalink_content(url, client, logger):
    """
    If the URL is a Slack permalink, fetch the content of the linked message.
    Returns the message text or a warning string if it fails.
    """
    match = re.search(r"slack\.com/archives/([A-Z0-9]+)/p(\d{16})", url)
    if not match:
        return None

    channel_id = match.group(1)
    timestamp_digits = match.group(2)
    message_ts = f"{timestamp_digits[:10]}.{timestamp_digits[10:]}"

    try:
        logger.info(f"Fetching content for Slack permalink: channel={channel_id}, ts={message_ts}")
        response = client.conversations_history(
            channel=channel_id,
            latest=message_ts,
            inclusive=True,
            limit=1
        )
        if response.get("messages"):
            return response["messages"][0].get("text")
        else:
            logger.warning(f"Message not found for permalink: {url}")
            return "[Warning: Linked message not found or has been deleted]"
    except Exception as e:
        logger.error(f"Failed to fetch Slack permalink content for {url}: {e}")
        if "not_in_channel" in str(e):
             return "[Warning: Could not fetch the linked message because I am not in that channel.]"
        return "[Warning: An error occurred while fetching the linked message.]"

def ask_to_translate_title(say, channel, ts, page_id, original_title, new_title, title_prop_name, notion_url):
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
                        "value": json.dumps({"page_id": page_id, "new_title": new_title, "title_prop_name": title_prop_name, "notion_url": notion_url})
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

def ask_to_translate_image(say, channel_id, thread_ts, file_obj):
    """Asks the user if they want to translate the image."""
    file_name = file_obj.get('name')
    file_id = file_obj.get('id')
    
    # Pass only the file_id to avoid exceeding the value length limit.
    value_payload = json.dumps({"file_id": file_id})

    say(
        channel=channel_id,
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"An image (`{file_name}`) was attached. Would you like me to analyze it and translate any text found within?"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Yes, translate"},
                        "style": "primary",
                        "action_id": "translate_image_confirm",
                        "value": value_payload
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "No"},
                        "style": "danger",
                        "action_id": "translate_image_cancel"
                    }
                ]
            }
        ]
    )

def ask_to_translate_og_image(say, channel_id, thread_ts, og_image_url, source_url):
    """Asks the user if they want to translate the og:image."""
    value_payload = json.dumps({"og_image_url": og_image_url})

    say(
        channel=channel_id,
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🖼️ A preview image was found for <{source_url}>. Would you like me to analyze it and translate any text found within?"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Yes, translate"},
                        "style": "primary",
                        "action_id": "translate_og_image_confirm",
                        "value": value_payload
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "No"},
                        "style": "danger",
                        "action_id": "translate_image_cancel" # We can reuse the cancel action
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
    notion_url = action_details.get('notion_url') # Retrieve the URL
    
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

        success_message = f"✅ Notion document title successfully changed to: *{new_title}*"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": success_message}}
        ]
        if notion_url:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"To translate the body, use the command: `/translate-notion {notion_url}`"}]}
            )

        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=blocks,
            text=success_message # Fallback for notifications
        )

    except Exception as e:
        logger.error(f"Error updating Notion page title: {e}")
        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[],
            text=f":warning: An error occurred while updating the Notion page title: ```{e}```"
        )

@app.action("translate_image_cancel")
def handle_translate_image_cancel(ack, body, logger):
    ack()
    try:
        app.client.chat_delete(channel=body['channel']['id'], ts=body['message']['ts'])
    except Exception as e:
        logger.error(f"Error deleting image translation prompt: {e}")

def _process_image_translation(channel_id, original_ts, image_url, image_name, logger, headers=None):
    """A helper function to process image translation and update Slack messages."""
    try:
        # Update message to "thinking" and remove buttons
        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":camera_with_flash: Analyzing image `{image_name}`... this may take a moment."
                    }
                }
            ],
            text="Analyzing image..."
        )

        analysis_result = analyze_image_from_url(image_url, logger, headers=headers)

        # Update message with the final result
        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[], # clear blocks
            text=analysis_result
        )
        logger.info(f"Successfully posted image translation for {image_name}")

    except Exception as e:
        logger.error(f"Error in _process_image_translation for {image_name}: {e}")
        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[],
            text=f":warning: An error occurred while processing the image translation: ```{e}```"
        )

@app.action("translate_image_confirm")
def handle_translate_image_confirm(ack, body, logger):
    ack()
    action_details = json.loads(body['actions'][0]['value'])
    file_id = action_details['file_id']
    
    original_ts = body['message']['ts']
    channel_id = body['channel']['id']

    try:
        # 1. Get file info using the file_id
        logger.info(f"Fetching file info for file_id: {file_id}")
        file_info_response = app.client.files_info(file=file_id)
        if not file_info_response.get("ok"):
            error_msg = file_info_response.get('error', 'Unknown error')
            logger.error(f"Failed to get file info for {file_id}: {error_msg}")
            app.client.chat_update(
                channel=channel_id,
                ts=original_ts,
                blocks=[],
                text=f":warning: Could not get image details to start translation. Error: `{error_msg}`"
            )
            return
        
        file_obj = file_info_response.get('file')
        image_url = file_obj.get('url_private_download')
        image_name = file_obj.get('name')
        headers = {'Authorization': f'Bearer {os.environ.get("SLACK_BOT_TOKEN")}'}

        if not image_url:
            logger.error("No private download URL found for the image.")
            app.client.chat_update(
                channel=channel_id,
                ts=original_ts,
                blocks=[],
                text=":warning: Could not translate image. No download URL found."
            )
            return

        _process_image_translation(channel_id, original_ts, image_url, image_name, logger, headers=headers)

    except Exception as e:
        logger.error(f"Error in handle_translate_image_confirm: {e}")
        app.client.chat_update(
            channel=channel_id,
            ts=original_ts,
            blocks=[],
            text=f":warning: An error occurred while processing the image translation: ```{e}```"
        )

@app.action("translate_og_image_confirm")
def handle_translate_og_image_confirm(ack, body, logger):
    ack()
    action_details = json.loads(body['actions'][0]['value'])
    og_image_url = action_details['og_image_url']
    
    original_ts = body['message']['ts']
    channel_id = body['channel']['id']
    
    image_name = f"preview image from <{og_image_url}>"
    _process_image_translation(channel_id, original_ts, og_image_url, image_name, logger)



# Notion Page Translation Functions

def fetch_all_blocks(page_id, logger):
    """Fetches all blocks from a Notion page, handling pagination."""
    all_blocks = []
    start_cursor = None
    while True:
        try:
            response = notion.blocks.children.list(block_id=page_id, start_cursor=start_cursor, page_size=100)
            all_blocks.extend(response.get('results', []))
            if not response.get('has_more'):
                break
            start_cursor = response.get('next_cursor')
        except Exception as e:
            logger.error(f"Error fetching blocks for page {page_id}: {e}")
            raise
    return all_blocks

def translate_text_chunk(text, target_language, logger):
    """Translates a single chunk of text using the generative model."""
    if not text.strip():
        return ""

    # Clean the text before translation: replace <URL|Text> with just Text
    cleaned_text = re.sub(r'<(https?://[^|]+)\|([^>]+)>', r'\\2', text)

    logger.info(f"Text chunk to be translated to {target_language}:\n{cleaned_text}")

    prompt = f'''You are a professional translator. Your task is to translate the text provided inside the <translate> XML tags.
- First, detect the source language of the text.
- Then, translate it to {target_language}.
- Preserve all original line breaks and spacing.
- Provide only the raw, translated text. Do not add any extra explanations, introductory phrases, or the XML tags.

<translate>
{cleaned_text}
</translate>
'''
    try:
        generation_config = GenerationConfig(max_output_tokens=2048)
        response = model.generate_content(prompt, generation_config=generation_config)
        
        if not response.candidates or not response.candidates[0].content.parts:
            logger.warning(f"Translation result was empty for target language {target_language}. It might have been blocked.")
            return "[Translation failed or was blocked by safety filters]"
        
        return response.text.strip()
    except Exception as e:
        logger.error(f"Error during text chunk translation to {target_language}: {e}")
        return f"[Translation Error] {e}"

def translate_text_chunks(texts, target_language, logger):
    """Translates a list of text chunks using the generative model."""
    if not texts:
        return []

    # Clean the texts before translation
    cleaned_texts = [re.sub(r'<(https?://[^|]+)\|([^>]+)>', r'\\2', text) for text in texts]

    # Create a single prompt with all the texts
    # Using a structured format (JSON) for input and output
    prompt = f"""You are a professional translator. Your task is to translate the following text chunks to {target_language}.
The text chunks are provided in a JSON array of strings.
Your response MUST be a JSON array of strings with the translated text chunks in the exact same order.
Preserve all original line breaks and spacing within each chunk.
Provide only the raw JSON array in your response.

{json.dumps(cleaned_texts, ensure_ascii=False)}
"""

    try:
        generation_config = GenerationConfig(
            max_output_tokens=8192,
        )
        response = model.generate_content(prompt, generation_config=generation_config)
        
        if not response.candidates or not response.candidates[0].content.parts:
            logger.warning(f"Translation result was empty for target language {target_language}. It might have been blocked.")
            return ["[Translation failed or was blocked by safety filters]"] * len(texts)
        
        # Clean the response text
        response_text = response.text.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        # Parse the JSON response
        logger.info(f"Cleaned response from translation model: {response_text}")
        translated_texts = json.loads(response_text)
        
        if len(translated_texts) != len(texts):
            logger.warning(f"Translated chunks count mismatch. Expected {len(texts)}, got {len(translated_texts)}. Falling back to one-by-one translation.")
            return [translate_text_chunk(text, target_language, logger) for text in texts]

        return translated_texts

    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error parsing JSON response from translation model: {e}. Falling back to one-by-one translation.")
        return [translate_text_chunk(text, target_language, logger) for text in texts]
    except Exception as e:
        logger.error(f"Error during batch text chunk translation to {target_language}: {e}")
        return [f"[Translation Error] {e}"] * len(texts)


def process_notion_translation(page_id, url, channel_id, thinking_message_ts, logger, target_language=None):
    """Processes the translation of a Notion page."""
    CHARACTER_LIMIT = 5000  # Character limit for each translation chunk

    try:
        # 1. Fetch original page to get title and parent
        logger.info(f"Fetching page details for {page_id}")
        original_page = notion.pages.retrieve(page_id=page_id)
        
        title_property, title_prop_name = None, None
        for prop_name, prop_details in original_page.get('properties', {}).items():
            if prop_details.get('type') == 'title':
                title_property, title_prop_name = prop_details, prop_name
                break
        
        if not title_property or not title_prop_name:
            app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=":warning: Could not find a title property for the given Notion page.")
            return

        original_title = title_property.get('title', [{}])[0].get('plain_text', 'Untitled')
        parent = original_page.get('parent')

        # 2. Fetch all blocks from the page
        logger.info(f"Fetching all blocks for page {page_id}")
        blocks = fetch_all_blocks(page_id, logger)
        if not blocks:
            app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=":warning: The Notion page appears to be empty. There's nothing to translate.")
            return
            
        # 3. Determine translation direction if not provided
        if not target_language:
            page_text_for_lang_detect = "".join(
                "".join(t.get('plain_text', '') for t in b.get(b.get('type'), {}).get('rich_text', []))
                for b in blocks[:10] if b.get('type') in ['paragraph', 'heading_1', 'heading_2', 'heading_3']
            )
            is_korean = any('가' <= char <= '힣' for char in page_text_for_lang_detect)
            target_language = "English" if is_korean else "Korean"
            logger.info(f"Determined translation direction: {'KR' if is_korean else 'EN'} -> {target_language}")

        new_title_suffix = f"_{target_language[:2].upper()}"

        # 4. Group and translate blocks
        new_blocks = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            block_type = block.get('type')

            if block_type in ['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'quote', 'callout', 'toggle']:
                current_group_blocks = []
                current_group_text_len = 0
                
                j = i
                while j < len(blocks) and blocks[j].get('type') == block_type:
                    block_to_add = blocks[j]
                    text_to_add = "".join([t.get('plain_text', '') for t in block_to_add.get(block_type, {}).get('rich_text', [])])
                    
                    if current_group_text_len + len(text_to_add) > CHARACTER_LIMIT and current_group_blocks:
                        break

                    current_group_blocks.append(block_to_add)
                    current_group_text_len += len(text_to_add)
                    j += 1

                original_texts = ["".join([t.get('plain_text', '') for t in b.get(block_type, {}).get('rich_text', [])]) for b in current_group_blocks]
                
                non_empty_texts = [text for text in original_texts if text.strip()]
                non_empty_indices = [idx for idx, text in enumerate(original_texts) if text.strip()]

                if non_empty_texts:
                    translated_texts_non_empty = translate_text_chunks(non_empty_texts, target_language, logger)
                    translated_texts = ["" for _ in original_texts]
                    for idx, translated_text in zip(non_empty_indices, translated_texts_non_empty):
                        translated_texts[idx] = translated_text
                else:
                    translated_texts = ["" for _ in original_texts]

                for original_block, translated_text in zip(current_group_blocks, translated_texts):
                    if translated_text.strip():
                        new_block = {
                            "object": "block",
                            "type": block_type,
                            block_type: { "rich_text": [{"type": "text", "text": {"content": translated_text}}] }
                        }
                        if block_type == 'callout':
                            if 'color' in original_block[block_type]:
                                new_block[block_type]['color'] = original_block[block_type]['color']
                            if 'icon' in original_block[block_type]:
                                new_block[block_type]['icon'] = original_block[block_type]['icon']
                        new_blocks.append(new_block)
                    else:
                        new_blocks.append(original_block)
                
                i = j

            elif block_type in ['divider', 'image', 'file', 'video', 'code']:
                new_blocks.append(block)
                i += 1
            else:
                new_blocks.append(block)
                i += 1

        # 5. Create the new page with the translated blocks
        # --- Translate Title Logic ---
        is_candidate_for_title_translation = re.search(r'[가-힣]', original_title) and not (
            re.search(r'[a-zA-Z]', original_title) and
            ('/' in original_title or re.search(r'\s*\([^)]+\)', original_title))
        )

        if target_language == "English" and is_candidate_for_title_translation:
            logger.info(f"Title '{original_title}' is a candidate for KR -> EN translation.")
            title_prompt = f"Translate the following Korean document title to English. Respond with only the translated title, without any additional text or quotation marks. Title: '{original_title}'"
            title_translation_response = model.generate_content(title_prompt)
            english_title = title_translation_response.text.strip().replace('"', '')
            new_title = f"{original_title} ({english_title})"
        else:
            logger.info(f"Title '{original_title}' will not be translated, appending suffix instead.")
            new_title = f"{original_title}{new_title_suffix}"
        
        logger.info(f"Creating new translated page for {page_id} with title '{new_title}'")
        logger.info(f"Creating new page in parent: {json.dumps(parent, indent=2)}")

        blocks_to_create = new_blocks[:100]
        
        new_page = notion.pages.create(
            parent=parent,
            properties={
                title_prop_name: {
                    "title": [{"text": {"content": new_title}}]
                }
            },
            children=blocks_to_create
        )
        logger.info(f"Successfully created new page with ID: {new_page['id']}")

        remaining_blocks = new_blocks[100:]
        for i in range(0, len(remaining_blocks), 100):
            chunk = remaining_blocks[i:i+100]
            logger.info(f"Appending block chunk {i//100 + 1} to page {new_page['id']}")
            notion.blocks.children.append(block_id=new_page['id'], children=chunk)

        # 6. Final success message
        app.client.chat_update(
            channel=channel_id,
            ts=thinking_message_ts,
            text=f"✅ Translation complete! A new page has been created:\n*<{new_page['url']}|{new_title}>*"
        )

    except notion_client.errors.APIResponseError as e:
        logger.error(f"Notion API Error during translation: {e}")
        if e.code == 'validation_error':
            logger.error(f"Validation error details: {e}")
        error_message = f":warning: A Notion API error occurred: `{e.code}`. I might not have the right permissions for the page or its parent."
        app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=error_message)
    except Exception as e:
        logger.error(f"An unexpected error occurred during Notion translation: {e}")
        error_message = f":warning: An unexpected error occurred: ```{{e}}```"
        app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=error_message)


@app.command("/translate-notion")
def handle_translate_notion(ack, command, say, logger):
    """Handles the /translate-notion command to translate a full Notion page while preserving formatting."""
    ack()

    text = command.get('text', '').strip()
    parts = text.split()
    url = ""
    target_language_arg = None

    if len(parts) > 0:
        url = parts[0]
    if len(parts) > 1:
        target_language_arg = parts[1]

    if not url or ('notion.so' not in url and 'notion.site' not in url):
        say(text="Please provide a valid Notion URL. Usage: `/translate-notion <notion_page_url> [target_language]`")
        return

    page_id = get_page_id_from_url(url)
    if not page_id:
        say(text="I couldn't find a valid Page ID in that URL. Please check the link and try again.")
        return

    thinking_message = say(text=f":hourglass_flowing_sand: Translating Notion page: <{url}>. This might take a moment...")
    
    process_notion_translation(page_id, url, command['channel_id'], thinking_message['ts'], logger, target_language=target_language_arg)

@app.command("/translate-notion-jp")
def handle_translate_notion_jp(ack, command, say, logger):
    """Handles the /translate-notion-jp command to translate a full Notion page to Japanese."""
    ack() 
    
    url = command.get('text', '').strip()
    if not url or ('notion.so' not in url and 'notion.site' not in url):
        say(text="Please provide a valid Notion URL. Usage: `/translate-notion-jp <notion_page_url>`")
        return

    page_id = get_page_id_from_url(url)
    if not page_id:
        say(text="I couldn\'t find a valid Page ID in that URL. Please check the link and try again.")
        return

    thinking_message = say(text=f":hourglass_flowing_sand: Translating Notion page to Japanese: <{url}>. This might take a moment...")
    
    process_notion_translation(page_id, url, command['channel_id'], thinking_message['ts'], logger, target_language="Japanese")

def analyze_image_from_url(image_url, logger, headers=None):
    """
    Downloads an image from a URL, analyzes it with Gemini, and returns the analysis.
    Returns the analysis text or an error string.
    """
    logger.info(f"Analyzing image from URL: {image_url}")
    try:
        response = requests.get(image_url, headers=headers or {}, timeout=20)
        response.raise_for_status()

        content_type = response.headers.get('Content-Type', '')
        if not content_type.startswith('image/'):
            logger.warning(f"URL did not return an image. Content-Type: {content_type}")
            return f":warning: The link ({image_url}) did not lead to a direct image file."

        image_data = response.content
        
        image_part = Part.from_data(data=image_data, mime_type=content_type)
        
        prompt = """You are a visual translation expert. Your task is to analyze the provided image and perform the following tasks:
1. Identify all distinct pieces of text in the image.
2. For each piece of text, describe its approximate location (e.g., 'top center', 'bottom left banner').
3. Provide the original text you identified.
4. Translate the text. If it's Korean, translate to English. For all other languages, translate to Korean.
5. Present the result as a Slack message using markdown. If no text is found, state that clearly.

Example Output:
---
🖼️ *Image Text Analysis & Translation*

*Location:* Top-left corner
> *Original:* 안녕하세요
> *Translation:* Hello

*Location:* Bottom, inside a blue button
> *Original:* Click Here
> *Translation:* 여기를 클릭하세요
---
"""
        
        response = model.generate_content([image_part, prompt])
        analysis_text = response.text.strip()

        if not analysis_text:
            return "I analyzed the image but could not find any text to translate."
        
        return analysis_text

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download image from {image_url}: {e}")
        return f":warning: Could not download the image from the link to analyze it."
    except Exception as e:
        logger.error(f"Error analyzing image from {image_url} with Gemini: {e}")
        return f":warning: An error occurred while analyzing the image from the link."

def create_url_summary_blocks(clean_url, logger):
    """
    Fetches a URL, summarizes its content, translates the summary.
    Returns a tuple of (Slack blocks for summary, og_image_url or None).
    """
    try:
        # 1. 웹페이지 콘텐츠 추출
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
        response = requests.get(clean_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        # OG:Image URL 추출
        og_image_url = None
        og_image_tag = soup.find('meta', property='og:image')
        if og_image_tag and og_image_tag.get('content'):
            og_image_url = urljoin(clean_url, og_image_tag['content'])
            logger.info(f"Found og:image for {clean_url}: {og_image_url}")

        page_text = soup.get_text(separator='\n', strip=True)
        
        # 2. 텍스트 요약
        summarization_prompt = f'''Please summarize the following article text in its original language. Focus on the main points and key information, and keep the summary concise, within 350 characters.
            Article Text:
            {page_text[:4000]}'''
        summary_response = model.generate_content(summarization_prompt)
        summary_text = summary_response.text.strip()

        # 3. 요약문 번역
        translation_prompt = f'''You are a professional translator. Detect the language of the following text and translate it.
- If it is Korean, translate it to English.
- For all other languages, translate it to Korean.
- Provide only the raw, translated text.

Text to translate:
{summary_text}'''
        translated_summary = model.generate_content(translation_prompt).text.strip()
        
        # 4. 블록 키트 메시지 구성
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🌐 *Summary & Translation for <{clean_url}>*: "
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": translated_summary
                }
            }
        ]
        
        return blocks, og_image_url

    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching URL {clean_url}: {e}")
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f":warning: Sorry, I couldn't fetch the content from <{clean_url}>."}}], None
    except Exception as e:
        logger.error(f"Error summarizing/translating URL {clean_url}: {e}")
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f":warning: Sorry, I couldn't summarize or translate the page at <{clean_url}>. Error: {e}"}}], None 

def handle_image_translation(file_obj, channel_id, thread_ts, say, logger, client):
    """Asks the user if they want to translate an image file."""
    mimetype = file_obj.get('mimetype', '')
    if not mimetype.startswith('image/'):
        logger.debug(f"Skipping non-image file: {file_obj.get('name')}")
        return

    logger.info(f"Detected image file, asking for translation: {file_obj.get('name')} in channel {channel_id}")
    ask_to_translate_image(say, channel_id, thread_ts, file_obj)

def translate_message(event, say, client, logger):
    """ 
    Translates a message from a user and posts the translation in a thread.
    If the message contains URLs, it also summarizes and translates them.
    """
    channel_id = event.get('channel')
    user_id = event.get('user')
    text = event.get('text')
    original_ts = event.get('ts')
    thread_ts_from_event = event.get('thread_ts')

    translation_thread_ts = thread_ts_from_event or original_ts
    thinking_response = None

    try:
        # 1. URL 패턴 정의 및 메시지에서 URL 찾기
        mention_pattern = r"<@\w+>"
        bracketed_url_pattern = r"<https?://[^>]+>"
        plain_url_pattern = r"https?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
        
        bracketed_urls = re.findall(bracketed_url_pattern, text)
        text_without_bracketed = re.sub(bracketed_url_pattern, '', text)
        plain_urls = re.findall(plain_url_pattern, text_without_bracketed)
        urls_in_message = bracketed_urls + plain_urls

        text_without_special_elements = re.sub(mention_pattern, '', text_without_bracketed)
        text_without_special_elements = re.sub(plain_url_pattern, '', text_without_special_elements)

        # CASE A: 일반 텍스트가 포함된 경우
        if text_without_special_elements.strip():
            thinking_response = say(text=":thought_balloon: Translating text...", thread_ts=translation_thread_ts)
            
            linked_content_for_translation = []
            warnings_to_post_as_reply = []
            placeholders = {}

            def replace_url_for_text_translation(match):
                url = match.group(0)
                placeholder = f"__URL_PLACEHOLDER_{len(placeholders)}__"
                placeholders[placeholder] = url
                
                if "slack.com" in url:
                    content = fetch_slack_permalink_content(url, client, logger)
                    if content:
                        if content.startswith("[Warning:"):
                            if "not in that channel" in content:
                                warnings_to_post_as_reply.append(f"I tried to translate the message at <{url}> as well, but I couldn\'t access it because I\'m not invited to that channel. 😢")
                        else:
                            linked_content_for_translation.append(content)
                return placeholder

            text_with_placeholders = re.sub(bracketed_url_pattern, replace_url_for_text_translation, text)
            text_with_placeholders = re.sub(plain_url_pattern, replace_url_for_text_translation, text_with_placeholders)

            full_text_to_translate = text_with_placeholders
            if linked_content_for_translation:
                quoted_section = "\n".join(linked_content_for_translation)
                full_text_to_translate = f"{text_with_placeholders}\n\n\nFrom Slack Link:\n{quoted_section}\n"

            prompt = f'''You are a professional translator. Your task is to translate the given text, including any quoted sections. 
- Detect the language of the text.
- If it is Korean, translate it to English.
- For all other languages, translate it to Korean.
- IMPORTANT: The text may contain placeholders like `__URL_PLACEHOLDER_0__`. You MUST preserve these placeholders in your translated output exactly as they are. Do not translate them.
- Preserve all original line breaks and spacing.
- Provide only the raw, translated text. Do not add any markdown formatting or other explanatory text.

Text to translate:
{full_text_to_translate}
'''
            translation_response = model.generate_content(prompt)
            translated_text_with_placeholders = translation_response.text.strip()

            final_translated_text = translated_text_with_placeholders
            for placeholder, url in placeholders.items():
                final_translated_text = final_translated_text.replace(placeholder, f"<{url.strip('<>')}>")
            
            is_korean = any(c >= '가' and c <= '힣' for c in text)
            reply_text = f"🌐 Translation (EN) from <@{user_id}>: {final_translated_text}" if is_korean else f"🌐 번역 (KR) from <@{user_id}>: {final_translated_text}"
            
            app.client.chat_update(channel=channel_id, ts=thinking_response['ts'], text=reply_text, unfurl_links=False)
            
            for warning in warnings_to_post_as_reply:
                say(channel=channel_id, text=warning, thread_ts=translation_thread_ts)

        # 후속 조치: 모든 경우에 대해 일반/Notion URL 처리
        thread_for_follow_ups = translation_thread_ts or (thinking_response and thinking_response.get('ts')) or original_ts
        
        for url in urls_in_message:
            clean_url = url.strip('<>').split('|')[0]
            
            if 'notion.so' in clean_url or 'notion.site' in clean_url:
                page_id = get_page_id_from_url(clean_url)
                if page_id:
                    try:
                        page = notion.pages.retrieve(page_id=page_id)
                        properties = page.get('properties', {})
                        title_property, title_prop_name = None, None
                        for prop_name, prop_details in properties.items():
                            if prop_details.get('type') == 'title':
                                title_property, title_prop_name = prop_details, prop_name
                                break
                        
                        if title_property and title_prop_name:
                            original_title = title_property.get('title', [{}])[0].get('plain_text', 'Untitled')
                            is_candidate = re.search(r'[가-힣]', original_title) and not (re.search(r'[a-zA-Z]', original_title) and ('/' in original_title or re.search(r'\s*\([^)]+\)', original_title)))
                            if is_candidate:
                                prompt = f"Translate the following Korean document title to English. Respond with only the translated title, without any additional text or quotation marks. Title: '{original_title}'"
                                title_translation_response = model.generate_content(prompt)
                                english_title = title_translation_response.text.strip()
                                new_title_format = f"{original_title} ({english_title})"
                                ask_to_translate_title(say, channel_id, thread_for_follow_ups, page_id, original_title, new_title_format, title_prop_name, clean_url)
                    except Exception as e:
                        logger.error(f"Error processing Notion link {clean_url}: {e}")

            elif 'slack.com' not in clean_url:
                summary_blocks, og_image_url = create_url_summary_blocks(clean_url, logger)
                if summary_blocks:
                    say(channel=channel_id, thread_ts=thread_for_follow_ups, blocks=summary_blocks, unfurl_links=False)
                if og_image_url:
                    ask_to_translate_og_image(say, channel_id, thread_for_follow_ups, og_image_url, clean_url)

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

def translate_bot_parent_message(event, say, logger):
    """Handles the translation of a parent message from a bot."""
    channel_id = event.get('channel')
    original_ts = event.get('ts')
    text = event.get('text', '')

    if not text.strip():
        return

    try:
        is_korean = any('가' <= char <= '힣' for char in text)
        target_language = "English" if is_korean else "Korean"
        translated_text = translate_text_chunk(text, target_language, logger)

        if not translated_text or "[Translation Error]" in translated_text or "[Translation failed" in translated_text:
            logger.warning(f"Skipping thread creation due to translation failure for parent message: {original_ts}")
            return

        result = say(text=translated_text)
        translated_ts = result.get('ts')
        if translated_ts:
            bot_thread_mapper.add_mapping(original_ts, translated_ts)
            logger.info(f"BOT_TRANSLATION: Created new translated thread. Original: {original_ts}, Translated: {translated_ts}")
    except Exception as e:
        logger.error(f"Error translating bot parent message {original_ts}: {e}")
        say(text=f":warning: Failed to translate the message due to an error: ```{e}```", thread_ts=original_ts)

def translate_bot_reply_message(event, say, logger):
    """Handles the translation of a reply message from a bot."""
    thread_ts = event.get('thread_ts')
    text = event.get('text', '')

    if not text.strip():
        return

    translated_thread_ts = bot_thread_mapper.get_translated_thread_ts(thread_ts)
    if not translated_thread_ts:
        logger.warning(f"BOT_TRANSLATION: Could not find a mapped translated thread for original thread {thread_ts}")
        return

    try:
        is_korean = any('가' <= char <= '힣' for char in text)
        target_language = "English" if is_korean else "Korean"
        translated_text = translate_text_chunk(text, target_language, logger)

        if not translated_text or "[Translation Error]" in translated_text or "[Translation failed" in translated_text:
            logger.warning(f"Skipping reply translation due to failure for text: {text[:50]}...")
            return

        say(text=translated_text, thread_ts=translated_thread_ts)
    except Exception as e:
        logger.error(f"Error translating bot reply message: {e}")

@app.event("message")
def handle_message_events(body, say, client, logger):
    event = body.get('event', {})
    
    # --- Message Origin Check ---
    if event.get("bot_id") == BOT_ID:
        return
    
    subtype = event.get("subtype")
    if subtype in ["message_changed", "message_deleted"]:
        return

    # --- Routing for Bot Messages ---
    if event.get("bot_id") or subtype == "bot_message":
        if event.get('thread_ts'):
            logger.info(f"--- New Bot Reply Event Received --- \nTEXT: {event.get('text')}")
            translate_bot_reply_message(event, say, logger)
        else:
            logger.info(f"--- New Parent Bot Event Received --- \nTEXT: {event.get('text')}")
            translate_bot_parent_message(event, say, logger)
        return 

    # --- Routing for User Messages ---
    if event.get("user"):
        channel_id = event.get('channel')
        channel_type = event.get('channel_type')
        is_registered = channel_manager.is_channel_registered(channel_id) or channel_type in ['im', 'mpim']
        
        if not is_registered:
            return

        # Handle text content if it exists
        if should_translate(event):
            logger.info(f"--- New User Text Event Received --- \nTEXT: {event.get('text')}")
            translate_message(event, say, client, logger)

        # Handle file attachments if they exist
        if 'files' in event:
            logger.info(f"--- New User File Event Received ---")
            thread_ts = event.get('thread_ts') or event.get('ts')
            for file_obj in event['files']:
                handle_image_translation(file_obj, channel_id, thread_ts, say, logger, client)


if __name__ == "__main__":
    logger.info("Starting bot...")
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
