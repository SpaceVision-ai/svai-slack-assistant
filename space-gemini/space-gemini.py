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
    # Handle URLs with hyphens in the page ID at the end
    match = re.search(r'-([a-f0-9]{32})$', url.split('?')[0])
    if match:
        return match.group(1)
    
    # Handle URLs where the ID is the last part of the path
    match = re.search(r'/([a-f0-9]{32})$', url.split('?')[0])
    if match:
        return match.group(1)

    # Handle standard Notion page URLs
    match = re.search(r'[a-f0-9]{32}', url)
    if match:
        return match.group(0)
        
    return None

def fetch_notion_page_content(page_id):
    """Fetches and returns the text content of a Notion page."""
    try:
        content = ""
        # Fetch blocks from the page
        blocks = notion.blocks.children.list(block_id=page_id)[ "results"]
        for block in blocks:
            if 'type' in block and block[block['type']].get('rich_text'):
                for text_item in block[block['type']]['rich_text']:
                    if text_item.get('plain_text'):
                        content += text_item['plain_text'] + '\n'
        return content
    except Exception as e:
        logger.error(f"Error fetching Notion page content for page_id {page_id}: {e}")
        return None

def find_last_notion_link_in_channel(client, channel_id):
    """Finds the last Notion link in the channel's recent history."""
    try:
        history_response = client.conversations_history(channel=channel_id, limit=20) # Check last 20 messages
        for message in history_response.get("messages", []):
            text = message.get("text", "")
            notion_urls = re.findall(r"https://www.notion.so/[\w\-./]+", text)
            if notion_urls:
                return notion_urls[0] # Return the first one found (which is the latest)
    except Exception as e:
        logger.error(f"Error searching for Notion link in channel {channel_id}: {e}")
    return None

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
                # Optionally, notify the user about the error
    return gemini_payload, extracted_texts

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
    """Gathers conversation history, asks Flash model to extract relevant context, and processes it."""
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
        return "", [], []

    # Create the structured conversation history
    structured_history = []
    for msg in messages:
        user_id = msg.get("user", "N/A")
        user_name = get_user_info(user_id) if user_id != "N/A" else "Bot/App"
        message_text = msg.get("text", "")
        message_ts = msg.get("ts", "")
        files = msg.get("files", [])
        file_info = [(f.get("id"), f.get("name")) for f in files]
        structured_history.append(f"(message_id='{message_ts}', author='{user_name}(<@{user_id}>)', message='''{message_text}''', files={file_info})")
    
    history_string = "\n".join(structured_history)

    # Ask Flash model to extract the necessary context
    try:
        flash_prompt = f"""You are a context analysis expert. From the 'Previous Slack Conversation' below, extract all message texts and file information (IDs and names) that are necessary to answer the 'User Prompt'.

        - Respond with only the extracted text and file details, or with an empty response if no context is needed.
        - Combine all text into a single block.
        - List all relevant file IDs and names in a structured format under a 'Files:' heading.

        [Previous Slack Conversation]
        {history_string}

        [User Prompt]
        {user_prompt}
        """
        
        flash_response = model_flash.generate_content(flash_prompt)
        extracted_context_text = flash_response.text
        logger.info(f"Flash model extracted context:\n{extracted_context_text}")

        # Process the files mentioned in the flash response
        gemini_payload = []
        if "Files:" in extracted_context_text:
            # Extract file IDs from the text
            file_ids_to_fetch = re.findall(r"(\\w+),", extracted_context_text)
            if file_ids_to_fetch:
                all_files_in_history = [file for msg in messages if "files" in msg for file in msg["files"]]
                files_to_process = [f for f in all_files_in_history if f.get("id") in file_ids_to_fetch]
                
                if files_to_process:
                    dummy_event = {"files": files_to_process}
                    # Note: process_attachments returns (payload, text_list), we only need the payload here
                    # The text from docx/txt will be in the extracted_context_text already
                    gemini_payload, _ = process_attachments(dummy_event)

        return extracted_context_text, gemini_payload, [] # Return empty list for extracted_texts for now

    except Exception as e:
        logger.error(f"Error during Flash model context extraction: {e}")
        return "", [], [] # Return empty on error

# --- Slack Event Handlers ---

@app.event("app_mention")
def handle_app_mention_events(body, say, client, logger):
    logger.info(body)
    try:
        event = body["event"]
        user_text = event.get("text", "").strip()
        channel_id = event["channel"]
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        thinking_message = say(text="Thinking...", thread_ts=thread_ts or message_ts, channel=channel_id)

        # Step 0: Check if context is needed
        needs_context = False
        try:
            check_prompt = f"Does the following user request require you to look at previous messages, threads, or files in the conversation for context? Answer with only 'True' or 'False'. Request: \"{user_text}\""
            response = model_flash.generate_content(check_prompt)
            if "true" in response.text.lower():
                needs_context = True
                logger.info("Flash model determined that context is needed.")
        except Exception as e:
            logger.error(f"Error with Flash model context check: {e}")
            # Default to assuming context is needed if the check fails
            needs_context = True 

        extracted_context = ""
        context_payload = []

        if needs_context:
            # Step 1 & 2: Extract context with Flash model
            extracted_context, context_payload, _ = extract_context_with_flash(client, channel_id, thread_ts, user_text)

        # Step 3: Process with Pro model
        # Process files attached to the *current* message as well
        gemini_payload, extracted_texts = process_attachments(event)
        gemini_payload.extend(context_payload)

        # Construct the final prompt for the Pro model
        prompt_text = (
            f"You are a helpful AI assistant, Space Gemini. Please answer the user's question based on the provided context.\n"
            f"When you mention a user, you MUST use their Slack user ID in the format <@USER_ID>.\n"
            f"--- Extracted Context from Conversation ---\n{extracted_context}\n\n"
            f"--- User's Original Question ---\n{user_text}"
        )

        full_prompt_text = "\n".join([prompt_text] + extracted_texts).strip()
        gemini_payload.insert(0, full_prompt_text)

        if not full_prompt_text and not gemini_payload:
             client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="I need a question or some text to go with the files.")
             return

        response = model_pro.generate_content(gemini_payload)
        handle_gemini_response(client, channel_id, thinking_message, response.text, thread_ts=thread_ts or message_ts)

    except Exception as e:
        logger.error(f"Error in app_mention handler: {e}")
        event = body.get("event", {})
        say(text=f"An error occurred: {e}", thread_ts=event.get("ts"), channel=event.get("channel"))




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