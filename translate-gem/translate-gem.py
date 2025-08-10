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
    text = event.get('text', '')
    if not text:
        return False

    # 1. Define patterns for special elements
    bracketed_url_pattern = r"<https?://[^>]+>"
    plain_url_pattern = r"https?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
    mention_pattern = r"<@\w+>"

    # 2. Check for the presence of normal text
    text_without_special_elements = re.sub(bracketed_url_pattern, '', text)
    text_without_special_elements = re.sub(plain_url_pattern, '', text_without_special_elements)
    text_without_special_elements = re.sub(mention_pattern, '', text_without_special_elements)

    # If there is any plain text, proceed with translation
    if text_without_special_elements.strip():
        channel_id = event.get('channel')
        channel_type = event.get('channel_type')
        return channel_manager.is_channel_registered(channel_id) or channel_type in ['im', 'mpim']

    # 3. If no plain text, check if any of the URLs are from Notion or Slack
    all_urls_found = re.findall(bracketed_url_pattern, text) + re.findall(plain_url_pattern, text)
    
    has_special_link = False
    for url in all_urls_found:
        # Extract the actual URL from formats like <url|text>
        clean_url = url.strip('<>').split('|')[0]
        if 'notion.so' in clean_url or 'notion.site' in clean_url or 'slack.com' in clean_url:
            has_special_link = True
            break
    
    # If a special link is found, proceed with translation
    if has_special_link:
        channel_id = event.get('channel')
        channel_type = event.get('channel_type')
        return channel_manager.is_channel_registered(channel_id) or channel_type in ['im', 'mpim']

    # Otherwise, do not translate
    return False


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
        return text

    source_language = "Korean" if target_language == "English" else "another language (e.g., English)"
    prompt = f"""You are a professional translator. Your task is to translate the text provided inside the <translate> XML tags.
- The source language is {source_language}.
- Translate it to {target_language}.
- Preserve all original line breaks and spacing.
- Provide only the raw, translated text. Do not add any extra explanations, introductory phrases, or the XML tags.

<translate>
{text}
</translate>
"""
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Error during text chunk translation: {e}")
        return f"[Translation Error] {text}" # Return original text with an error marker

@app.command("/translate-notion")
def handle_translate_notion(ack, command, say, logger):
    """Handles the /translate-notion command to translate a full Notion page while preserving formatting."""
    ack()
    
    url = command.get('text', '').strip()
    if not url or ('notion.so' not in url and 'notion.site' not in url):
        say(text="Please provide a valid Notion URL. Usage: `/translate-notion <notion_page_url>`")
        return

    page_id = get_page_id_from_url(url)
    if not page_id:
        say(text="I couldn't find a valid Page ID in that URL. Please check the link and try again.")
        return

    thinking_message = say(text=f":hourglass_flowing_sand: Translating Notion page: <{url}>. This might take a moment...")
    
    try:
        # 1. Fetch original page to get title and parent
        logger.info(f"Fetching page details for {page_id}")
        original_page = notion.pages.retrieve(page_id=page_id)
        
        title_property = None
        title_prop_name = None
        for prop_name, prop_details in original_page.get('properties', {}).items():
            if prop_details.get('type') == 'title':
                title_property = prop_details
                title_prop_name = prop_name
                break
        
        if not title_property or not title_prop_name:
            app.client.chat_update(channel=command['channel_id'], ts=thinking_message['ts'], text=":warning: Could not find a title property for the given Notion page.")
            return

        original_title = title_property.get('title', [{}])[0].get('plain_text', 'Untitled')
        parent = original_page.get('parent')

        # 2. Fetch all blocks from the page
        logger.info(f"Fetching all blocks for page {page_id}")
        blocks = fetch_all_blocks(page_id, logger)
        if not blocks:
            app.client.chat_update(channel=command['channel_id'], ts=thinking_message['ts'], text=":warning: The Notion page appears to be empty. There's nothing to translate.")
            return
            
        # 3. Determine translation direction from the first few text blocks
        page_text_for_lang_detect = "".join(
            "".join(t.get('plain_text', '') for t in b.get(b.get('type'), {}).get('rich_text', []))
            for b in blocks[:10] if b.get('type') in ['paragraph', 'heading_1', 'heading_2', 'heading_3']
        )
        is_korean = any('가' <= char <= '힣' for char in page_text_for_lang_detect)
        target_language = "English" if is_korean else "Korean"
        new_title_suffix = "_EN" if is_korean else "_KR"
        logger.info(f"Determined translation direction: {'KR' if is_korean else 'EN'} -> {target_language}")

        # 4. Translate block by block, preserving structure
        new_blocks = []
        for block in blocks:
            block_type = block.get('type')
            
            # A. Handle text-based blocks that need translation
            if block_type in ['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'quote', 'callout', 'toggle']:
                original_text = "".join([t.get('plain_text', '') for t in block.get(block_type, {}).get('rich_text', [])])
                
                if original_text.strip():
                    translated_text = translate_text_chunk(original_text, target_language, logger)
                    
                    new_block = {
                        "object": "block",
                        "type": block_type,
                        block_type: { "rich_text": [{"type": "text", "text": {"content": translated_text}}] }
                    }
                    # Preserve special properties like color and icon
                    if block_type == 'callout':
                        if 'color' in block[block_type]:
                            new_block[block_type]['color'] = block[block_type]['color']
                        if 'icon' in block[block_type]:
                            new_block[block_type]['icon'] = block[block_type]['icon']
                    new_blocks.append(new_block)
                else:
                    # Preserve empty blocks (e.g., empty lines)
                    new_blocks.append(block)

            # B. Handle non-text blocks that should be copied as-is
            elif block_type in ['divider', 'image', 'file', 'video', 'code']:
                # For 'code' blocks, we don't translate the code itself.
                new_blocks.append(block)
            
            # C. Other block types are currently ignored but could be added here.

        # 5. Create the new page with the translated blocks
        logger.info(f"Creating new translated page for {page_id}")

        # --- Translate Title Logic ---
        # Check if the title is primarily Korean and not already in a bilingual format.
        is_candidate_for_title_translation = re.search(r'[가-힣]', original_title) and not (
            re.search(r'[a-zA-Z]', original_title) and
            ('/' in original_title or re.search(r'\s*\([^)]+\)', original_title))
        )

        if is_candidate_for_title_translation:
            logger.info(f"Title '{original_title}' is a candidate for KR -> EN translation.")
            title_prompt = f"Translate the following Korean document title to English. Respond with only the translated title, without any additional text or quotation marks. Title: '{original_title}'"
            title_translation_response = model.generate_content(title_prompt)
            english_title = title_translation_response.text.strip().replace('"', '') # Remove quotes just in case
            # Create the new title in "Korean (English)" format
            new_title = f"{original_title} ({english_title})"
        else:
            logger.info(f"Title '{original_title}' will not be translated, appending suffix instead.")
            # If not a candidate for translation, just append the suffix.
            new_title = f"{original_title}{new_title_suffix}"
        
        # The children array for a page create request can have a maximum of 100 blocks.
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

        # If there are more blocks, append them in chunks of 100.
        remaining_blocks = new_blocks[100:]
        for i in range(0, len(remaining_blocks), 100):
            chunk = remaining_blocks[i:i+100]
            logger.info(f"Appending block chunk {i//100 + 1} to page {new_page['id']}")
            notion.blocks.children.append(block_id=new_page['id'], children=chunk)

        # 6. Final success message
        app.client.chat_update(
            channel=command['channel_id'],
            ts=thinking_message['ts'],
            text=f"✅ Translation complete! A new page has been created:\n*<{new_page['url']}|{new_title}>*"
        )

    except notion_client.errors.APIResponseError as e:
        logger.error(f"Notion API Error during translation: {e}")
        error_message = f":warning: A Notion API error occurred: `{e.code}`. I might not have the right permissions for the page or its parent."
        app.client.chat_update(channel=command['channel_id'], ts=thinking_message['ts'], text=error_message)
    except Exception as e:
        logger.error(f"An unexpected error occurred during Notion translation: {e}")
        error_message = f":warning: An unexpected error occurred: ```{e}```"
        app.client.chat_update(channel=command['channel_id'], ts=thinking_message['ts'], text=error_message)


def translate_message(event, say, client, logger):


    """
    메시지를 번역하고 올바른 위치(채널 또는 스레드)에 게시합니다.
    Notion 또는 Slack 링크가 포함된 경우, 해당 내용을 가져와 함께 번역합니다.
    """
    channel_id = event.get('channel')
    user_id = event.get('user')
    text = event.get('text')
    original_ts = event.get('ts')
    thread_ts_from_event = event.get('thread_ts')
    channel_type = event.get('channel_type')

    # 1. 번역 메시지를 보낼 스레드를 결정합니다.
    translation_thread_ts = None
    if channel_type not in ['im', 'mpim']:  # 공개/비공개 채널인 경우
        translation_thread_ts = thread_ts_from_event or original_ts
    # DM/MPIM인 경우, None으로 유지하여 새 메시지로 보냅니다.

    thinking_response = None
    try:
        # 2. 모든 사용자에게 보이는 "생각 중" 메시지를 보냅니다.
        thinking_messages = [
            "Interpreting Heptapod Language…", "Translating to Mentalese…",
            "Analyzing linguistic patterns…", "Connecting to the universal translator…"
        ]
        thinking_message_text = random.choice(thinking_messages)
        
        thinking_response = say(
            text=f":thought_balloon: {thinking_message_text}",
            thread_ts=translation_thread_ts
        )

        # 3. URL을 placeholder로 바꾸고, 링크된 콘텐츠를 가져옵니다.
        url_pattern = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
        urls = []
        linked_content_for_translation = []
        warnings_to_post_as_reply = []

        def replace_url(match):
            url = match.group(0)
            urls.append(url)
            placeholder = f"__URL_PLACEHOLDER_{len(urls)-1}__"
            
            # 슬랙 링크 내용 가져오기
            content = fetch_slack_permalink_content(url, client, logger)
            if content:
                if content.startswith("[Warning:"):
                    if "not in that channel" in content:
                        warning_message = f"I tried to translate the message at <{url}> as well, but I couldn't access it because I'm not invited to that channel. 😢"
                        warnings_to_post_as_reply.append(warning_message)
                else:
                    linked_content_for_translation.append(content)
            return placeholder
        
        text_with_placeholders = re.sub(url_pattern, replace_url, text)

        # 인용된 내용과 원본 텍스트를 결합 (원본이 위로)
        full_text_to_translate = text_with_placeholders
        if linked_content_for_translation:
            quoted_section = "\n".join(linked_content_for_translation)
            full_text_to_translate = f"{text_with_placeholders}\n\n\nFrom Slack Link:\n{quoted_section}\n"

        # 4. 번역을 수행합니다.
        if re.sub(r'__URL_PLACEHOLDER_\d+__', '', full_text_to_translate).strip():
            prompt = f"""You are a professional translator. Your task is to translate the given text, including any quoted sections. 
- Detect the language of the text.
- If it is Korean, translate it to English.
- For all other languages, translate it to Korean.
- IMPORTANT: The text may contain placeholders like `__URL_PLACEHOLDER_0__`. You MUST preserve these placeholders in your translated output exactly as they are. Do not translate them.
- Preserve all original line breaks and spacing.
- Provide only the raw, translated text. Do not add any markdown formatting or other explanatory text.

Text to translate:
{full_text_to_translate}
"""
            
            translation_response = model.generate_content(prompt)
            translated_text_with_placeholders = translation_response.text.strip()

            final_translated_text = translated_text_with_placeholders
            for i, url in enumerate(urls):
                final_translated_text = final_translated_text.replace(f"__URL_PLACEHOLDER_{i}__", url)
        else:
            final_translated_text = ' '.join(urls)

        is_korean = any(c >= '가' and c <= '힣' for c in text)
        if is_korean:
            reply_text = f"🌐 Translation (EN) from <@{user_id}>: {final_translated_text}"
        else:
            reply_text = f"🌐 번역 (KR) from <@{user_id}>: {final_translated_text}"

        # 5. "생각 중" 메시지를 최종 번역 결과로 업데이트합니다.
        app.client.chat_update(
            channel=channel_id,
            ts=thinking_response['ts'],
            text=reply_text
        )
        
        # 7. 후속 조치(경고, Notion 제안)를 위한 스레드를 결정하고 실행합니다.
        thread_for_follow_ups = None
        if channel_type not in ['im', 'mpim']: # 채널
            thread_for_follow_ups = translation_thread_ts
        else: # DM/MPIM
            thread_for_follow_ups = thinking_response['ts']

        # 슬랙 링크 경고 메시지 전송
        for warning in warnings_to_post_as_reply:
            say(channel=channel_id, text=warning, thread_ts=thread_for_follow_ups)

        # Notion 링크 확인 및 처리
        logger.info(f"Found URLs for Notion processing: {urls}")
        for url in urls:
            if 'notion.so' in url or 'notion.site' in url:
                page_id = get_page_id_from_url(url)
                logger.info(f"Processing Notion URL: {url}, Extracted Page ID: {page_id}")
                if page_id:
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
                            logger.info(f"Page Title for {page_id}: '{original_title}'")
                            
                            # --- New logic to check if a title should be translated ---
                            
                            # A title should be translated if it contains Korean but does not appear to be bilingually formatted.
                            is_candidate = re.search(r'[가-힣]', original_title)
                            is_bilingual_formatted = False

                            # Check for bilingual patterns only if both languages are present.
                            if is_candidate and re.search(r'[a-zA-Z]', original_title):
                                # Pattern 1: Contains a slash, indicating format like "Korean / English"
                                if '/' in original_title:
                                    is_bilingual_formatted = True
                                
                                # Pattern 2: Contains parentheses, indicating format like "Korean (English)"
                                if re.search(r'\s*\([^)]+\)', original_title):
                                    is_bilingual_formatted = True

                            logger.info(f"Title check for {page_id}: Is candidate? {bool(is_candidate)}, Is bilingual formatted? {is_bilingual_formatted}")

                            if is_candidate and not is_bilingual_formatted:
                                logger.info(f"Condition met for {page_id}, proceeding with translation.")
                                prompt = f"Translate the following Korean document title to English. Respond with only the translated title, without any additional text or quotation marks. Title: '{original_title}'"
                                title_translation_response = model.generate_content(prompt)
                                english_title = title_translation_response.text.strip()
                                new_title_format = f"{original_title} ({english_title})"
                                
                                ask_to_translate_title(say, channel_id, thread_for_follow_ups, page_id, original_title, new_title_format, title_prop_name, url)
                            else:
                                logger.info(f"Skipping translation suggestion for {page_id} as conditions were not met.")
                        else:
                            logger.warning(f"Could not find a title property for page ID: {page_id}")

                    except notion_client.errors.APIResponseError as e:
                        if e.code == "object_not_found":
                            error_message_kr = (
                                f":warning: <{url}|Notion 페이지>에 접근할 수 없습니다. "
                                "페이지가 존재하지 않거나, 저에게 접근 권한이 없는 것 같아요."
                            )
                            error_message_en = (
                                f":warning: I can't access that <{url}|Notion page>. "
                                "It might not exist, or I may not have permission."
                            )
                            say(channel=channel_id, thread_ts=thread_for_follow_ups, text=f"{error_message_kr}\n\n{error_message_en}")
                        else:
                            logger.error(f"Notion API Error for url {url}: {e}")
                            say(channel=channel_id, thread_ts=thread_for_follow_ups, text=f":warning: An error occurred with the Notion API for <{url}>: ```{e}```")
                    except Exception as e:
                        logger.error(f"An unexpected error occurred while handling Notion link {url}: {e}")
                        say(channel=channel_id, thread_ts=thread_for_follow_ups, text=f":warning: An unexpected error occurred while processing the Notion link <{url}>: ```{e}```")
                else:
                    logger.info(f"No page ID found for URL: {url}")

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
def handle_message_events(body, say, client, logger):
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
            translate_message(event_data, say, client, logger)

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