import os
import logging
import time
import requests
import io
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import vertexai
from vertexai.generative_models import GenerativeModel, Part
import docx
from PyPDF2 import PdfReader
import re
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

# --- User Info Cache ---
user_cache = {}

def get_user_info(user_id):
    if user_id not in user_cache:
        try:
            response = app.client.users_info(user=user_id)
            user_cache[user_id] = response["user"]["real_name"] or response["user"]["name"]
        except Exception as e:
            logger.error(f"Error fetching user info for {user_id}: {e}")
            user_cache[user_id] = user_id # Fallback to user_id
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
    match = re.search(r'-([a-f0-9]{32})$', url.split('?')[0])
    if match:
        return match.group(1)
    match = re.search(r'/([a-f0-9]{32})$', url.split('?')[0])
    if match:
        return match.group(1)
    match = re.search(r'[a-f0-9]{32}', url)
    if match:
        return match.group(0)
    return None

def fetch_notion_page_content(page_id):
    """Fetches and returns the text content of a Notion page."""
    try:
        content = ""
        # Fetch blocks from the page
        blocks = notion.blocks.children.list(block_id=page_id)("results")
        for block in blocks:
            if 'type' in block and block[block['type']].get('rich_text'):
                for text_item in block[block['type']]['rich_text']:
                    if text_item.get('plain_text'):
                        content += text_item['plain_text'] + '\n'
        return content
    except Exception as e:
        logger.error(f"Error fetching Notion page content for page_id {page_id}: {e}")
        return None

def fetch_website_content(url):
    """Fetches and returns the text content of a general website."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        # Remove script and style elements
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        # Get text
        text = soup.get_text()
        # Break into lines and remove leading/trailing space on each
        lines = (line.strip() for line in text.splitlines())
        # Break multi-headlines into a line each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        # Drop blank lines
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
                if mime_type.startswith(("image/", "video/")) or mime_type == "application/pdf":
                    gemini_payload.append(Part.from_data(content, mime_type=mime_type))
                elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                    extracted_texts.append(f"---\n{file_info.get('name')}\n---\n{extract_text_from_docx(content)}")
                elif mime_type.startswith("text/"):
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

    # 1. Add the base message text
    if message_text:
        text_parts.append(message_text)

    # 2. Process attached files
    if "files" in message:
        payload, texts = process_attachments(message)
        payload_parts.extend(payload)
        text_parts.extend(texts)

    # 3. Process all http/https links
    urls = re.findall(r'<?(https?://[\w\-./?=&%#]+)>?', message_text)
    for url in set(urls): # Use set to avoid processing duplicate URLs
        if 'notion.so' in url or 'notion.site' in url:
            page_id = get_page_id_from_url(url)
            if page_id:
                content = fetch_notion_page_content(page_id)
                if content:
                    text_parts.append(f"\n--- Content from Notion URL ({url}) ---\n{content}\n")
        elif 'slack.com' in url:
            match = re.search(r"archives/([A-Z0-9]+)/p(\d{16})", url)
            if match:
                channel_id, ts_digits = match.groups()
                ts = f"{ts_digits[:10]}.{ts_digits[10:]}"
                try:
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
            # General website
            content = fetch_website_content(url)
            text_parts.append(f"\n--- Content from Website ({url}) ---\n{content}\n")

    return "\n".join(text_parts), payload_parts

def handle_gemini_response(client, channel_id, thinking_message, text, thread_ts=None):
    SLACK_MSG_LIMIT = 4000
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
    """Gathers conversation history, asks Flash model to identify relevant messages, and processes them."""
    messages = []
    if thread_ts:
        try:
            result = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=50)
            messages = result.get("messages", [])
        except Exception as e:
            logger.error(f"Error fetching thread replies for context extraction: {e}")
    else:
        try:
            result = client.conversations_history(channel=channel_id, limit=20)
            messages = result.get("messages", [])
        except Exception as e:
            logger.error(f"Error fetching channel history for context extraction: {e}")

    if not messages:
        return "", []

    structured_history = []
    for msg in messages:
        user_name = get_user_info(msg.get("user", "N/A"))
        has_files = bool(msg.get('files'))
        message = msg.get("text", "")
        structured_history.append(f"(message_id='{msg.get('ts')}', author='{user_name}', message='{message}', has_files={has_files})")
    
    history_string = "\n".join(structured_history)

    try:
        flash_prompt = f"""You are a context analysis expert. From the 'Previous Slack Conversation' below, identify which message_ids are relevant to answer the 'User Prompt'.

        - Respond with only a comma-separated list of the necessary message_ids (e.g., '1629888000.000100,1629888001.000200').
        - If no context is needed, respond with an empty string.

        [Previous Slack Conversation]
        {history_string}

        [User Prompt]
        {user_prompt}
        """
        
        flash_response = model_flash.generate_content(flash_prompt)
        relevant_message_ids = [ts.strip() for ts in flash_response.text.split(',') if ts.strip()]

        final_text_parts = []
        final_payload_parts = []
        relevant_messages = [msg for msg in messages if msg.get("ts") in relevant_message_ids]
        
        for msg in relevant_messages:
            text, payload = process_message_for_context(msg, client)
            final_text_parts.append(text)
            final_payload_parts.extend(payload)

        return "\n".join(final_text_parts), final_payload_parts

    except Exception as e:
        logger.error(f"Error during Flash model context extraction: {e}")
        return "", []

# --- Slack Event Handlers ---

@app.event("app_mention")
def handle_app_mention_events(body, say, client, logger):
    try:
        event = body["event"]
        # Ignore messages from bots
        if event.get("bot_id"):
            return
            
        user_text = event.get("text", "").strip()
        channel_id = event["channel"]
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        thinking_message = say(text="Thinking...", thread_ts=thread_ts or message_ts, channel=channel_id)

        needs_context = False
        try:
            check_prompt = f'''Does the following user request require you to look at previous messages, threads, or files in the conversation for context? Answer with only 'True' or 'False'. Request: "{user_text}"'''
            response = model_flash.generate_content(check_prompt)
            if "true" in response.text.lower():
                needs_context = True
                logger.info("Flash model determined that context is needed.")
        except Exception as e:
            logger.error(f"Error with Flash model context check: {e}")
            needs_context = True

        extracted_context_text = ""
        context_payload = []

        if needs_context:
            client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="Checking previous context... 🧐")
            extracted_context_text, context_payload = extract_context_with_flash(client, channel_id, thread_ts, user_text)

        client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="Generating response... 🤔")
        
        current_message_text, current_payload = process_message_for_context(event, client)
        
        gemini_payload = context_payload + current_payload

        prompt_text = (
            f"You are a helpful AI assistant, Space Gemini. Please answer the user's question based on the provided context.\n"
            f"When you mention a user, you MUST use their Slack user ID in the format <@USER_ID>.\n"
            f"--- Extracted Context from Conversation ---\n{extracted_context_text}\n\n"
            f"--- User's Original Question ---\n{current_message_text}"
        )

        gemini_payload.insert(0, prompt_text)

        if not user_text and not gemini_payload:
             client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="I need a question or some text to go with the files.")
             return

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
