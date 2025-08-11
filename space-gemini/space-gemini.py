import os
import logging
import time
import requests
import io
import json
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import vertexai
from vertexai.generative_models import GenerativeModel, Part
import docx
from PyPDF2 import PdfReader
import re
from datetime import datetime, timedelta
import notion_client
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# .env 파일 (봇 특정 및 공통) 로드
load_dotenv() # 현재 디렉터리의 .env 로드
load_dotenv(dotenv_path="../.env") # 상위 디렉터리의 .env 로드

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")

# Initializes your app with your bot token and socket mode handler
app = App(token=SLACK_BOT_TOKEN)

# Initialize Vertex AI
vertexai.init(project=os.environ.get("GOOGLE_CLOUD_PROJECT"), location=os.environ.get("GOOGLE_CLOUD_LOCATION"))

# Use a model that supports multimodal inputs
model_pro = GenerativeModel("gemini-2.5-pro")
model_flash = GenerativeModel("gemini-2.5-flash")

# Initialize Notion Client
notion = notion_client.Client(auth=os.environ.get("NOTION_API_KEY"))

# --- Constants ---
CHANNEL_HISTORY_LIMIT = 50 # Number of recent messages to check in a channel
DEFAULT_CONTEXT_DAYS = 3 # Default number of days to look back for context

# --- User Info Cache ---
user_cache = {}

def get_user_info(user_id):
    if not user_id or user_id == "N/A":
        return {"name": "N/A", "tz": None}
    if user_id not in user_cache:
        try:
            response = app.client.users_info(user=user_id)
            user_info = response["user"]
            user_cache[user_id] = {
                "name": user_info.get("real_name") or user_info.get("name"),
                "tz": user_info.get("tz")
            }
        except Exception as e:
            logger.error(f"Error fetching user info for {user_id}: {e}")
            user_cache[user_id] = {
                "name": user_id,
                "tz": None
            } # Fallback
    return user_cache[user_id]

# --- Helper Functions ---

def download_file(url, token):
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, stream=True)
    response.raise_for_status()
    return response.content

def extract_text_from_docx(content):
    try:
        with io.BytesIO(content) as doc_stream:
            doc = docx.Document(doc_stream)
            return "\n".join([para.text for para in doc.paragraphs])
    except Exception as e:
        logger.error(f"Error extracting text from DOCX: {e}")
        return ""

def extract_text_from_pdf(content):
    try:
        with io.BytesIO(content) as pdf_stream:
            reader = PdfReader(pdf_stream)
            return "\n".join([page.extract_text() for page in reader.pages])
    except Exception as e:
        logger.error(f"Error extracting text from PDF: {e}")
        return ""

def get_page_id_from_url(url):
    """Extract Notion page ID from a URL."""
    match = re.search(r'-([a-f0-9]{32})', url.split('?')[0])
    if match:
        return match.group(1)
    match = re.search(r'/([a-f0-9]{32}/)', url.split('?')[0])
    if match:
        return match.group(1)
    match = re.search(r'[a-f0-9]{32}', url)
    if match:
        return match.group(0)
    return None

def fetch_notion_page_content(page_id):
    """
    Fetches and returns the text content of a Notion page, including nested blocks.
    """
    try:
        all_text = []
        _fetch_blocks_recursively(page_id, all_text, 0)
        return "\n".join(all_text)
    except Exception as e:
        logger.error(f"Error starting Notion page fetch for page_id {page_id}: {e}")
        return None

def _fetch_blocks_recursively(block_id, text_list, indent_level):
    """
    A recursive helper function to fetch all blocks and their children from Notion.
    """
    try:
        response = notion.blocks.children.list(block_id=block_id)
        blocks = response.get("results", [])
        
        for block in blocks:
            block_text = ""
            if 'type' in block and block[block['type']].get('rich_text'):
                for text_item in block[block['type']]['rich_text']:
                    if text_item.get('plain_text'):
                        block_text += text_item['plain_text']
            
            if block_text:
                indent = "  " * indent_level
                text_list.append(f"{indent}- {block_text}")

            if block.get("has_children"):
                _fetch_blocks_recursively(block["id"], text_list, indent_level + 1)
                
    except Exception as e:
        logger.error(f"Error fetching children for Notion block {block_id}: {e}")

def fetch_website_content(url):
    """Fetches and returns the text content of a general website."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = '\n'.join(chunk for chunk in chunks if chunk)
        return text
    except Exception as e:
        logger.error(f"Error fetching website content for {url}: {e}")
        return f"[Warning: Could not fetch content from {url}]"

def process_attachments(event):
    gemini_payload = []
    extracted_texts = []
    if event.get("files"):
        for file_info in event["files"]:
            try:
                mime_type = file_info.get("mimetype", "application/octet-stream")
                content = download_file(file_info["url_private_download"], SLACK_BOT_TOKEN)
                if mime_type.startswith(("image/", "video/")):
                    logger.info(f"Adding image/video attachment to context: {file_info.get('name')}")
                    gemini_payload.append(Part.from_data(content, mime_type=mime_type))
                elif mime_type == "application/pdf":
                    logger.info(f"Extracting and adding content from PDF to context: {file_info.get('name')}")
                    extracted_texts.append(f"---\n{file_info.get('name')}\n---\n{extract_text_from_pdf(content)}")
                elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                    logger.info(f"Extracting and adding content from DOCX to context: {file_info.get('name')}")
                    extracted_texts.append(f"---\n{file_info.get('name')}\n---\n{extract_text_from_docx(content)}")
                elif mime_type.startswith("text/"):
                    logger.info(f"Adding content from text file to context: {file_info.get('name')}")
                    extracted_texts.append(f"---\n{file_info.get('name')}\n---\n{content.decode('utf-8')}")
                else:
                    logger.warning(f"Skipping unsupported file type: {mime_type}")
            except Exception as e:
                logger.error(f"Error processing file {file_info.get('name')}: {e}")
    return gemini_payload, extracted_texts

def process_message_for_context(message, client):
    """Processes a single Slack message to extract text, file content, and link content."""
    text_parts = []
    payload_parts = []
    message_text = message.get("text", "")

    if message_text:
        text_parts.append(message_text)

    if "files" in message:
        payload, texts = process_attachments(message)
        payload_parts.extend(payload)
        text_parts.extend(texts)

    urls = re.findall(r'<?(https?://[\w\-./?=&%#]+)>?', message_text)
    for url in set(urls):
        if 'notion.so' in url or 'notion.site' in url:
            page_id = get_page_id_from_url(url)
            if page_id:
                logger.info(f"Adding content from Notion URL to context: {url}")
                content = fetch_notion_page_content(page_id)
                if content:
                    text_parts.append(f"\n--- Content from Notion URL ({url}) ---\n{content}\n")
        elif 'slack.com' in url:
            match = re.search(r"archives/([A-Z0-9]+)/p(\d{16})", url)
            if match:
                channel_id, ts_digits = match.groups()
                ts = f"{ts_digits[:10]}.{ts_digits[10:]}"
                try:
                    logger.info(f"Adding content from Slack link to context: {url}")
                    response = client.conversations_history(channel=channel_id, latest=ts, inclusive=True, limit=1)
                    if response.get("messages"):
                        linked_message = response["messages"][0]
                        text, payload = process_message_for_context(linked_message, client)
                        text_parts.append(f"\n--- Content from Slack link ({url}) ---\n{text}\n")
                        payload_parts.extend(payload)
                except Exception as e:
                    logger.error(f"Failed to fetch Slack permalink content for {url}: {e}")
                    text_parts.append(f"\n[Warning: Could not fetch content for Slack link {url}]\n")
        else:
            logger.info(f"Adding content from Website to context: {url}")
            content = fetch_website_content(url)
            text_parts.append(f"\n--- Content from Website ({url}) ---\n{content}\n")

    return "\n".join(text_parts), payload_parts

def handle_gemini_response(client, channel_id, thinking_message, text, thread_ts=None):
    SLACK_MSG_LIMIT = 3000
    thinking_ts = thinking_message['ts']
    try:
        if len(text) <= SLACK_MSG_LIMIT:
            blocks = [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": text or "(empty response)"
                }
            }]
            client.chat_update(
                channel=channel_id,
                ts=thinking_ts,
                blocks=blocks,
                text="Gemini AI has generated a response."
            )
        else:
            logger.info(f"Response is too long ({len(text)} chars). Creating a file.")
            file_path = f"gemini_response_{int(time.time())}.md"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                client.chat_delete(channel=channel_id, ts=thinking_ts)
                client.files_upload_v2(
                    channel=channel_id, file=file_path, title="Gemini AI Response",
                    initial_comment="The response was too long, so I've attached it as a file. 📄", thread_ts=thread_ts
                )
            finally:
                os.remove(file_path)
    except Exception as e:
        logger.error(f"Error in handle_gemini_response: {e}")
        client.chat_postMessage(text=f"An error occurred: {e}", thread_ts=thread_ts, channel=channel_id)

def extract_context_with_flash(client, channel_id, thread_ts, user_prompt):
    """
    Gathers conversation history, asks Flash model to identify relevant messages,
    and processes them into a structured list. This includes content from attachments and links.
    """
    start_time = None
    latest_time = None
    try:
        period_prompt = f'''Analyze the user's request to determine the time period for a conversation history search. 
        Today's date is {datetime.now().strftime('%Y-%m-%d')}. Respond in a structured JSON format.

        Your JSON output MUST contain the following keys:
        1. `period_start`: A string representing the start date and time in "YYYY-MM-DD HH:MM:SS" format. If no start date is mentioned, respond with "none".
        2. `period_end`: A string representing the end date and time in "YYYY-MM-DD HH:MM:SS" format. If no end date is mentioned, respond with "none".

        User Request: "{user_prompt}"
        '''
        period_response = model_flash.generate_content(period_prompt)

        json_match = re.search(r'```json\n(.*)\n```', period_response.text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            analysis = json.loads(json_str)
            period_start_str = analysis.get("period_start")
            period_end_str = analysis.get("period_end")
            logger.info(f"Parsed period_start: {period_start_str}")
            logger.info(f"Parsed period_end: {period_end_str}")

            if period_start_str and period_start_str != "none":
                try:
                    start_time = datetime.strptime(period_start_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    start_time = datetime.strptime(period_start_str, "%Y-%m-%d") # Fallback to date only
            
            if period_end_str and period_end_str != "none":
                try:
                    latest_time = datetime.strptime(period_end_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    # Fallback to date only, and set to end of day
                    latest_time = datetime.strptime(period_end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    except Exception as e:
        logger.error(f"Error during Flash model period analysis: {e}")

    messages = []
    if thread_ts:
        try:
            result = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=100)
            messages = result.get("messages", [])
            parent_message_ts = thread_ts
            if not any(msg.get('ts') == parent_message_ts for msg in messages):
                logger.info(f"Parent message {parent_message_ts} not in recent replies, fetching it explicitly.")
                parent_message_response = client.conversations_history(
                    channel=channel_id, latest=parent_message_ts, inclusive=True, limit=1
                )
                if parent_message_response.get("messages"):
                    messages.insert(0, parent_message_response["messages"][0])
        except Exception as e:
            logger.error(f"Error fetching thread replies for context extraction: {e}")
    else:
        try:
            history_params = {
                'channel': channel_id,
                'limit': 1000
            }
            if start_time:
                history_params['oldest'] = str(start_time.timestamp())
                logger.info(f"Fetching channel history since {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            if latest_time:
                history_params['latest'] = str(latest_time.timestamp())
                logger.info(f"Fetching channel history until {latest_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            history_result = client.conversations_history(**history_params)
            top_level_messages = history_result.get("messages", [])
            all_messages = []
            processed_thread_ts = set()
            for msg in top_level_messages:
                all_messages.append(msg)
                thread_ts_val = msg.get("thread_ts")
                if thread_ts_val and thread_ts_val not in processed_thread_ts:
                    processed_thread_ts.add(thread_ts_val)
                    logger.info(f"Found thread {thread_ts_val} in channel history, fetching its replies.")
                    try:
                        replies_result = client.conversations_replies(channel=channel_id, ts=thread_ts_val)
                        thread_messages = replies_result.get("messages", [])
                        all_messages.extend(thread_messages)
                    except Exception as e:
                        logger.error(f"Error fetching replies for thread {thread_ts_val}: {e}")
            unique_messages_dict = {msg['ts']: msg for msg in all_messages}
            messages = list(unique_messages_dict.values())
            messages.sort(key=lambda x: float(x['ts']), reverse=True)
        except Exception as e:
            logger.error(f"Error fetching channel history and threads: {e}")

    if not messages:
        return []

    structured_history = []
    for msg in messages:
        user_info = get_user_info(msg.get("user", "N/A"))
        user_name = user_info.get("name")
        has_files = "has attachments or links" if msg.get('files') or 'http' in msg.get('text', '') else ""
        message_preview = msg.get("text", "")[:150] 
        structured_history.append(f"(message_id='{msg.get('ts')}', author='{user_name}', message='{message_preview}...', info='{has_files}')")
    
    history_string = "\n".join(structured_history)

    try:
        flash_prompt = f"""You are a context analysis expert. From the 'Previous Slack Conversation' below, identify which message_ids are essential for answering the 'User Prompt'. Consider messages with key info, questions, decisions, and attachments. Respond with only a comma-separated list of the necessary message_ids (e.g., '1629888000.000100,1629888001.000200'). If no context is needed, respond with an empty string.

[Previous Slack Conversation]
{history_string}

[User Prompt]
{user_prompt}
"""
        flash_response = model_flash.generate_content(flash_prompt)
        relevant_message_ids = {ts.strip() for ts in flash_response.text.split(',') if ts.strip()}

        if not relevant_message_ids:
            return []

        final_structured_context = []
        relevant_messages = [msg for msg in messages if msg.get("ts") in relevant_message_ids]
        
        for msg in relevant_messages:
            text, payload = process_message_for_context(msg, client)
            final_structured_context.append({
                "author_id": msg.get("user", "N/A"),
                "ts": msg.get("ts"),
                "text": text,
                "payload": payload
            })

        return final_structured_context

    except Exception as e:
        logger.error(f"Error during Flash model context extraction: {e}")
        return []



# --- Slack Event Handlers ---

@app.event("app_mention")
def handle_app_mention_events(body, say, client, logger):
    try:
        event = body["event"]
        if event.get("bot_id"):
            return
            
        user_text = event.get("text", "").strip()
        channel_id = event["channel"]
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        thinking_message = say(text="Thinking...", thread_ts=thread_ts or message_ts, channel=channel_id)

        try:
            check_prompt = f"Does the following user request require you to look at previous messages, threads, files, or links in the conversation for context? Answer with only 'True' or 'False'.\n\nUser Request: \"{user_text}\""
            response = model_flash.generate_content(check_prompt)
            logger.info(f"Flash model 'context_needed' check response: {response.text}")
            needs_context = "true" in response.text.lower()
        except Exception as e:
            logger.error(f"Error with Flash model context_needed check: {e}")
            needs_context = True # Default to true on error

        past_messages_structured = []
        if needs_context:
            client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="Checking previous context... 🧐")
            past_messages_structured = extract_context_with_flash(
                client, channel_id, thread_ts, user_text
            )

        client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="Generating response... 🤔")
        
        current_message_text, current_payload = process_message_for_context(event, client)
        current_message_structured = {
            "author_id": event.get("user", "N/A"),
            "ts": event.get("ts"),
            "text": current_message_text,
            "payload": current_payload
        }

        prompt_parts = [
            "You are a helpful AI assistant, Space Gemini. Please answer the user's question based on the provided conversation history.",
            "When you mention a user, you MUST use their Slack user ID in the format <@USER_ID>.",
            "IMPORTANT: Format your response for Slack. Do not use `**text**` for bolding. If you need to use emphasis, use `*text*` for bold or `_text_` for italics."
        ]
        gemini_payload = []

        if past_messages_structured:
            prompt_parts.append("--- Relevant Conversation History ---")
            sorted_past_messages = sorted(past_messages_structured, key=lambda x: float(x['ts']))
            for msg in sorted_past_messages:
                readable_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(msg['ts'])))
                author_mention = f"<@{msg['author_id']}>"
                prompt_parts.append(f"Message from {author_mention} ({readable_time}):\n{msg['text']}")
                gemini_payload.extend(msg['payload'])
        
        prompt_parts.append("\n--- User's Current Question ---")
        current_readable_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(current_message_structured['ts'])))
        current_author_mention = f"<@{current_message_structured['author_id']}>"
        prompt_parts.append(f"Message from {current_author_mention} ({current_readable_time}):\n{current_message_structured['text']}")
        gemini_payload.extend(current_message_structured['payload'])

        final_prompt_text = "\n\n".join(prompt_parts)
        gemini_payload.insert(0, final_prompt_text)

        if not user_text and not any(p for p in gemini_payload if not isinstance(p, str)):
             client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="I need a question or some text to go with the files.")
             return

        logger.info("--- Sending payload to Gemini Pro ---")
        logger.info(f"Prompt Text:\n{final_prompt_text}")
        num_parts = len([p for p in gemini_payload if not isinstance(p, str)])
        if num_parts > 0:
            logger.info(f"Additional Parts: {num_parts} (files/images/etc.)")
        logger.info("------------------------------------")

        response = model_pro.generate_content(gemini_payload)
        handle_gemini_response(client, channel_id, thinking_message, response.text, thread_ts=thread_ts or message_ts)

    except Exception as e:
        logger.error(f"Error in app_mention handler: {e}")
        event = body.get("event", {})
        say(text=f"An error occurred: {e}", thread_ts=event.get("ts"), channel=event.get("channel"))



@app.event("message")
def handle_direct_messages(body, say, client, logger):
    logger.info(body)
    try:
        event = body["event"]
        if event.get("bot_id") or (event.get("subtype") and event.get("subtype") != "file_share"):
            return
        if event.get("channel_type") == "im":
            handle_app_mention_events(body, say, client, logger)
    except Exception as e:
        logger.error(f"Error handling direct message: {e}")
        event = body.get("event", {})
        say(text=f"An error occurred: {e}", channel=event.get("channel"))

if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()