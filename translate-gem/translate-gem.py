import os
import logging
import json
import random
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic
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

# Anthropic 클라이언트 초기화
# TRANSLATION_MODEL 환경변수로 모델 변경 가능 (기본값: claude-haiku-4-5-20251001)
# 예) TRANSLATION_MODEL=claude-sonnet-4-6 으로 설정하면 Sonnet 사용
TRANSLATION_MODEL = os.environ.get("TRANSLATION_MODEL", "claude-haiku-4-5-20251001")
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
logger.info(f"Using translation model: {TRANSLATION_MODEL}")

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
                text="Hello! I\'ve been invited to this direct message channel and will now automatically translate messages. No extra commands needed!"
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

    # Clean the text before translation
    cleaned_text = re.sub(r'<(https?://[^|]+)\|([^>]+)>', r'\\2', text)
    logger.info(f"Text chunk to be translated to {target_language}:\n{cleaned_text}")

    # Simplified direct prompt
    prompt = f'''You are a professional translator. Your task is to translate the following text to {target_language}.
- Preserve all original line breaks and spacing.
- Provide only the raw, translated text. Do not add any extra explanations or introductory phrases.

Text to translate:
{cleaned_text}
'''
    try:
        response = anthropic_client.messages.create(
            model=TRANSLATION_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )

        if not response.content:
            logger.warning(f"Translation result was empty for target language {target_language}.")
            return "[Translation failed or was blocked by safety filters]"

        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Error during text chunk translation to {target_language}: {e}")
        return f"[Translation Error] {e}"

def translate_text_chunks(texts, target_language, logger):
    """Translates a list of text chunks using a simplified, non-JSON prompt."""
    if not texts:
        return []

    # Clean the texts before translation
    cleaned_texts = [re.sub(r'<(https?://[^|]+)\|([^>]+)>', r'\\2', text) for text in texts]

    # Define a unique separator
    separator = "---[GEMINI-TRANSLATE-BOUNDARY]---"

    # Create a single prompt with all texts joined by the separator
    combined_text = f"\n{separator}\n".join(cleaned_texts)

    prompt = f"""You are a professional translator. Your task is to translate the following text segments to {target_language}.
The segments are separated by a unique boundary string: `{separator}`.
Your response MUST contain the translated segments, separated by the exact same boundary string.
Preserve all original line breaks and spacing within each segment.
Provide only the raw, translated text with the separators. Do not add any other explanations.

Text to translate:
{combined_text}
"""

    try:
        response = anthropic_client.messages.create(
            model=TRANSLATION_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}]
        )

        if not response.content:
            logger.warning(f"Batch translation result was empty for target language {target_language}.")
            return ["[Translation failed or was blocked by safety filters]"] * len(texts)

        response_text = response.content[0].text
        translated_texts = response_text.split(separator)

        # Trim leading/trailing whitespace that might result from the split
        translated_texts = [text.strip() for text in translated_texts]
        
        if len(translated_texts) != len(texts):
            logger.warning(f"Translated chunks count mismatch. Expected {len(texts)}, got {len(translated_texts)}. Falling back to one-by-one translation.")
            # Fallback to individual translation if the batch fails
            return [translate_text_chunk(text, target_language, logger) for text in texts]

        return translated_texts

    except Exception as e:
        logger.error(f"Error during batch text chunk translation to {target_language}: {e}. Falling back to one-by-one translation.")
        # Fallback to individual translation on any exception
        return [translate_text_chunk(text, target_language, logger) for text in texts]

def collect_texts_recursively(source_block_id, logger, text_collection):
    """
    Recursively traverses blocks to collect all translatable text content.
    Appends {'id': block_id, 'text': text_content} dicts to text_collection list.
    """
    try:
        original_blocks = fetch_all_blocks(source_block_id, logger)
        for block in original_blocks:
            block_type = block.get('type')
            if block_type in ['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'quote', 'callout', 'toggle']:
                text_content = "".join([t.get('plain_text', '') for t in block.get(block_type, {}).get('rich_text', [])])
                if text_content.strip():
                    text_collection.append({'id': block['id'], 'text': text_content})
            
            if block.get('has_children'):
                collect_texts_recursively(block['id'], logger, text_collection)
    except Exception as e:
        logger.error(f"Error collecting texts from block {source_block_id}: {e}")

def build_page_recursively(source_block_id, target_block_id, translated_text_map, logger):
    """
    Recursively rebuilds the page structure using the translated text map,
    maintaining a 1:1 block correspondence to ensure recursion indexes are correct.
    """
    original_blocks = fetch_all_blocks(source_block_id, logger)
    if not original_blocks:
        return

    new_blocks_for_api = []
    recursion_tasks = []

    for i, block in enumerate(original_blocks):
        block_type = block.get('type')
        new_block = None

        # Reconstruct translatable block types
        if block_type in ['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'quote', 'callout', 'toggle']:
            translated_text = translated_text_map.get(block['id'])
            rich_text = [{"type": "text", "text": {"content": translated_text}}] if translated_text else block[block_type].get('rich_text', [])
            
            new_block = {"object": "block", "type": block_type, block_type: {"rich_text": rich_text}}
            if block_type == 'callout':
                if 'color' in block[block_type]: new_block[block_type]['color'] = block[block_type]['color']
                if 'icon' in block[block_type]: new_block[block_type]['icon'] = block[block_type]['icon']
        
        # Reconstruct non-text blocks that are copied as-is
        elif block_type in ['divider', 'image', 'file', 'video', 'code']:
            new_block = {"object": "block", "type": block_type, block_type: block[block_type]}
        
        # If block type is unsupported or failed, create an empty paragraph to maintain index
        if not new_block:
            new_block = {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}

        new_blocks_for_api.append(new_block)

        if block.get('has_children'):
            recursion_tasks.append({'original_id': block['id'], 'index': i})

    # Append new blocks to the target
    appended_blocks_results = []
    if new_blocks_for_api:
        for i in range(0, len(new_blocks_for_api), 100):
            chunk = new_blocks_for_api[i:i+100]
            logger.info(f"Appending {len(chunk)} blocks to target {target_block_id}")
            try:
                response = notion.blocks.children.append(block_id=target_block_id, children=chunk)
                appended_blocks_results.extend(response.get('results', []))
            except Exception as e:
                logger.error(f"Failed to append block chunk: {e}")
                # Add placeholders to keep index integrity
                appended_blocks_results.extend([None] * len(chunk))

    # Recurse for blocks with children
    for task in recursion_tasks:
        if task['index'] < len(appended_blocks_results):
            new_parent_block = appended_blocks_results[task['index']]
            if new_parent_block:
                build_page_recursively(task['original_id'], new_parent_block['id'], translated_text_map, logger)
            else:
                logger.warning(f"Skipping recursion for block at index {task['index']} as it failed to be created.")

def process_notion_translation(page_id, url, channel_id, thinking_message_ts, logger, target_language=None):
    """Processes the translation of a Notion page using a two-pass recursive method."""
    try:
        # 1. Fetch original page details
        logger.info(f"Fetching page details for {page_id}")
        original_page = notion.pages.retrieve(page_id=page_id)
        title_property, title_prop_name = next(((prop_details, prop_name) for prop_name, prop_details in original_page.get('properties', {}).items() if prop_details.get('type') == 'title'), (None, None))

        if not title_property:
            app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=":warning: Could not find a title property for the page.")
            return

        original_title = title_property.get('title', [{}])[0].get('plain_text', 'Untitled')
        parent = original_page.get('parent')

        # 2. Determine translation direction
        if not target_language:
            text_collection_for_lang_detect = []
            collect_texts_recursively(page_id, logger, text_collection_for_lang_detect)
            page_text_for_lang_detect = " ".join(item['text'] for item in text_collection_for_lang_detect[:10])
            is_korean = any('가' <= char <= '힣' for char in page_text_for_lang_detect)
            target_language = "English" if is_korean else "Korean"
            logger.info(f"Determined translation direction -> {target_language}")

        # 3. Translate Title
        new_title_suffix = f"_{target_language[:2].upper()}"
        is_candidate = re.search(r'[가-힣]', original_title) and not (re.search(r'[a-zA-Z]', original_title) and ('/' in original_title or re.search(r'\s*\([^)]+\)', original_title)))
        if target_language == "English" and is_candidate:
            prompt = f"Translate the following Korean document title to English. Respond with only the translated title. Title: '{original_title}'"
            english_title = anthropic_client.messages.create(
                model=TRANSLATION_MODEL,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            ).content[0].text.strip().replace('"', '')
            new_title = f"{original_title} ({english_title})"
        else:
            new_title = f"{original_title}{new_title_suffix}"

        # 4. Create the new empty page
        logger.info(f"Creating new page with title '{new_title}'")
        new_page = notion.pages.create(parent=parent, properties={title_prop_name: {"title": [{"text": {"content": new_title}}]}})
        new_page_id = new_page['id']
        logger.info(f"Created new empty page: {new_page_id}")

        # 5. Start Two-Pass Translation Process
        # Pass 1: Collect all texts
        logger.info("Pass 1: Collecting all translatable text.")
        text_collection = []
        collect_texts_recursively(page_id, logger, text_collection)

        if not text_collection:
            # If there's no text, still copy the structure
            logger.info("No text found to translate, copying structure only.")
            build_page_recursively(page_id, new_page_id, {}, logger)
            app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=f"✅ Translation complete, but no text was found to translate. The structure has been copied.\n*<{new_page['url']}|{new_title}>*")
            return

        # Pass 2: Batch translate
        logger.info(f"Pass 2: Translating {len(text_collection)} text blocks.")
        original_texts = [item['text'] for item in text_collection]
        translated_texts = translate_text_chunks(original_texts, target_language, logger)
        translated_text_map = {item['id']: translated_texts[i] for i, item in enumerate(text_collection)}

        # Pass 3: Recursively build the new page
        logger.info("Pass 3: Rebuilding the translated page structure.")
        build_page_recursively(page_id, new_page_id, translated_text_map, logger)

        # 6. Final success message
        app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=f"✅ Translation complete! A new page has been created:\n*<{new_page['url']}|{new_title}>*")

    except notion_client.errors.APIResponseError as e:
        logger.error(f"Notion API Error during translation: {e}")
        error_message = f":warning: A Notion API error occurred: `{e.code}`. Possible reasons:\n- I might lack permissions for the original page or its parent location.\n- The page might be empty or have an unsupported structure."
        if e.code == 'validation_error':
            logger.error(f"Validation error details: {e.body}")
            error_message += f"\nDetails: ```{e.body}```"
        app.client.chat_update(channel=channel_id, ts=thinking_message_ts, text=error_message)
    except Exception as e:
        logger.error(f"An unexpected error occurred during Notion translation: {e}", exc_info=True)
        error_message = f":warning: An unexpected error occurred: ```{e}```"
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
        say(text="I couldn\'t find a valid Page ID in that URL. Please check the link and try again.")
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


def create_url_summary_blocks(clean_url, logger):
    """
    Fetches a URL, summarizes its content, translates the summary.
    Returns a tuple of (Slack blocks for summary, og_image_url or None).
    """
    try:
        # 1. 웹페이지 콘텐츠 추출
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        response = requests.get(clean_url, headers=headers, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        # OG:Image URL 추출
        og_image_url = None
        og_image_tag = soup.find('meta', property='og:image')
        if og_image_tag and og_image_tag.get('content'):
            og_image_url = urljoin(clean_url, og_image_tag['content'])
            logger.info(f"Found og:image for {clean_url}: {og_image_url}")

        page_text = soup.get_text(separator='\n', strip=True)

        # 2. 요약 + 번역을 단일 API 호출로 처리
        summarize_and_translate_prompt = f'''You are a professional summarizer and translator. Given the article text below, do the following in a single response:

1. Summarize the article in its original language. Keep it concise, within 350 characters.
2. Translate that summary:
   - If the summary is in Korean, translate it to English.
   - For all other languages, translate it to Korean.

Respond in exactly this format (no extra text):
SUMMARY: <summary in original language>
TRANSLATION: <translated summary>

Article Text:
{page_text[:4000]}'''
        combined_response = anthropic_client.messages.create(
            model=TRANSLATION_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": summarize_and_translate_prompt}]
        ).content[0].text.strip()

        # 응답 파싱
        summary_text = ""
        translated_summary = ""
        for line in combined_response.splitlines():
            if line.startswith("SUMMARY:"):
                summary_text = line[len("SUMMARY:"):].strip()
            elif line.startswith("TRANSLATION:"):
                translated_summary = line[len("TRANSLATION:"):].strip()
        # 파싱 실패 시 전체 응답을 번역으로 사용
        if not translated_summary:
            translated_summary = combined_response

        # 3. 블록 키트 메시지 구성
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
        logger.warning(f"Skipping URL summary (fetch failed) {clean_url}: {e}")
        return [], None
    except Exception as e:
        logger.error(f"Error summarizing/translating URL {clean_url}: {e}")
        return [], None


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
            translation_response = anthropic_client.messages.create(
                model=TRANSLATION_MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}]
            )
            translated_text_with_placeholders = translation_response.content[0].text.strip()

            final_translated_text = translated_text_with_placeholders
            for placeholder, url in placeholders.items():
                final_translated_text = final_translated_text.replace(placeholder, f"<{url.strip('<>')}>")
            
            is_korean = any(c >= '가' and c <= '힣' for c in text)
            reply_text = f"🌐 Translation (EN) from <@{user_id}>: {final_translated_text}" if is_korean else f"🌐 번역 (KR) from <@{user_id}>: {final_translated_text}"
            
            app.client.chat_update(channel=channel_id, ts=thinking_response['ts'], text=reply_text, unfurl_links=False)
            
            for warning in warnings_to_post_as_reply:
                say(channel=channel_id, text=warning, thread_ts=translation_thread_ts)

        # 후속 조치: URL 처리를 백그라운드 스레드에서 병렬 실행
        thread_for_follow_ups = translation_thread_ts or (thinking_response and thinking_response.get('ts')) or original_ts

        def process_single_url(url):
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
                                title_translation_response = anthropic_client.messages.create(
                                    model=TRANSLATION_MODEL,
                                    max_tokens=256,
                                    messages=[{"role": "user", "content": prompt}]
                                )
                                english_title = title_translation_response.content[0].text.strip()
                                new_title_format = f"{original_title} ({english_title})"
                                ask_to_translate_title(say, channel_id, thread_for_follow_ups, page_id, original_title, new_title_format, title_prop_name, clean_url)
                    except Exception as e:
                        logger.error(f"Error processing Notion link {clean_url}: {e}")

            elif 'slack.com' not in clean_url:
                summary_blocks, og_image_url = create_url_summary_blocks(clean_url, logger)
                if summary_blocks:
                    say(channel=channel_id, thread_ts=thread_for_follow_ups, blocks=summary_blocks, unfurl_links=False)

        if urls_in_message:
            def run_url_processing():
                with ThreadPoolExecutor(max_workers=min(len(urls_in_message), 5)) as executor:
                    executor.map(process_single_url, urls_in_message)
            threading.Thread(target=run_url_processing, daemon=True).start()

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
    if subtype in ["message_changed", "message_deleted", "channel_join", "channel_leave"]:
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



if __name__ == "__main__":
    logger.info("Starting bot...")
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()