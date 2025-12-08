import os
import logging
import time
import requests
import io
import json
import threading
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

# Initialize Notion Client
notion = notion_client.Client(auth=os.environ.get("NOTION_API_KEY"))

# --- SpaceVision Context ---
SPACEVISION_CONTEXT = ""
SPACEVISION_HW_CONTEXT = ""

try:
    with open("docs/spacevision_technical_overview_summary.md", "r", encoding="utf-8") as f:
        SPACEVISION_CONTEXT = f.read()
    logger.info("Successfully loaded SpaceVision context.")
except FileNotFoundError:
    logger.warning("docs/spacevision_technical_overview_summary.md not found. Continuing without it.")
except Exception as e:
    logger.error(f"Error loading SpaceVision context: {e}")

try:
    with open("docs/SpaceVision_AI_HW_Datasheet_Summary.md", "r", encoding="utf-8") as f:
        SPACEVISION_HW_CONTEXT = f.read()
    logger.info("Successfully loaded SpaceVision HW context.")
except FileNotFoundError:
    logger.warning("docs/SpaceVision_AI_HW_Datasheet_Summary.md not found. Continuing without it.")
except Exception as e:
    logger.error(f"Error loading SpaceVision HW context: {e}")

# Use a model that supports multimodal inputs
system_prompt = f"""You are a helpful AI assistant, Space-Gemini.
When you mention a user, you MUST use their Slack user ID in the format <@USER_ID>.
IMPORTANT: Format your response for Slack. Do not use `**text**` for bolding. Use `*text*` for bold or `_text_` for italics.

[SpaceVision Technical Overview]
The following is the technical overview and business context of SpaceVision. Use this information to answer user questions relevant to the company's technology and business.
{{SPACEVISION_CONTEXT}}

[SpaceVision AI Hardware Datasheet]
The following is the summary of AI hardware equipment used by SpaceVision. Refer to this when answering questions about hardware specs, GPUs, servers, or equipment availability.
{{SPACEVISION_HW_CONTEXT}}
"""

model_pro = GenerativeModel("gemini-3-pro-preview", system_instruction=system_prompt)
model_flash = GenerativeModel("gemini-2.5-flash")

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
                client.chat_delete(channel=channel_id, ts=thinking_message["ts"])
                client.files_upload_v2(
                    channel=channel_id, file=file_path, title="Gemini AI Response",
                    initial_comment="The response was too long, so I've attached it as a file. 📄", thread_ts=thread_ts
                )
            finally:
                os.remove(file_path)
    except Exception as e:
        logger.error(f"Error in handle_gemini_response: {e}")
        client.chat_postMessage(text=f"An error occurred: {e}", thread_ts=thread_ts, channel=channel_id)

def get_search_criteria_from_prompt(user_prompt):
    """Analyzes the user prompt to find search criteria like period and scope."""
    start_time, latest_time = None, None
    search_scope, channel_ids = "CURRENT_CHANNEL", []

    try:
        period_prompt = f'''Analyze the user's request to determine the scope and time period for a Slack conversation search. Today's date is {datetime.now().strftime('%Y-%m-%d')}. Respond in a structured JSON format.

Your JSON output MUST contain the following keys:
1. `search_scope`: A string indicating the search area. Possible values are: "ENTIRE_WORKSPACE", "SPECIFIC_CHANNELS", "CURRENT_THREAD", "CURRENT_CHANNEL". Default to "CURRENT_CHANNEL" if not specified.
2. `channel_ids`: A list of Slack channel IDs (e.g., "C1234567") mentioned in the request. Extract this from channel links. If none, provide an empty list [].
3. `period_start`: A string representing the start date and time in "YYYY-MM-DD HH:MM:SS" format. If none, respond with "none".
4. `period_end`: A string representing the end date and time in "YYYY-MM-DD HH:MM:SS" format. If none, respond with "none".

User Request: "{user_prompt}"'''
        logger.info(f"--- Sending Scope/Period Prompt to Flash Model ---{period_prompt}------------------------------------")
        period_response = model_flash.generate_content(period_prompt)
        logger.info(f"--- Flash Model Scope/Period Analysis Result ---{period_response.text}------------------------------------")

        json_match = re.search(r'```json\n(.*)\n```', period_response.text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            analysis = json.loads(json_str)
            
            search_scope = analysis.get("search_scope", "CURRENT_CHANNEL")
            channel_ids = analysis.get("channel_ids", [])
            period_start_str = analysis.get("period_start")
            period_end_str = analysis.get("period_end")
            logger.info(f"Parsed period_start from Flash model: {period_start_str}")
            logger.info(f"Parsed period_end from Flash model: {period_end_str}")

            if period_start_str and period_start_str != "none":
                try:
                    start_time = datetime.strptime(period_start_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    start_time = datetime.strptime(period_start_str, "%Y-%m-%d")
            
            if period_end_str and period_end_str != "none":
                try:
                    latest_time = datetime.strptime(period_end_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    latest_time = datetime.strptime(period_end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    except Exception as e:
        logger.error(f"Error during Flash model scope/period analysis: {e}")
    
    return start_time, latest_time, search_scope, channel_ids

def summarize_content(content, user_prompt, source_name, logger, model_flash):
    """Summarizes text content from a file or URL based on the user's prompt."""
    if len(content.split()) < 50:
        logger.info(f"Content from '{source_name}' is too short to summarize, using full content.")
        return content
    try:
        prompt = f"""The following text is from the source '{source_name}'. Please read it and provide a concise summary that is relevant to the user's original request.

**IMPORTANT**: Your summary MUST be less than 4000 characters.

[User Request]
{user_prompt}

[Source Content]
{content[:15000]}

[Summary]
"""
        logger.info(f"Summarizing content from '{source_name}' for user prompt: '{user_prompt}'")
        response = model_flash.generate_content(prompt)
        logger.info(f"Finished summarizing content from '{source_name}'.")
        return response.text
    except Exception as e:
        logger.error(f"Error summarizing content from {source_name}: {e}")
        return f"[Could not summarize content from {source_name}. Truncated content follows]\n{content[:2000]}..."



def extract_context_with_flash(client, channel_id, thread_ts, user_prompt, search_scope, channel_ids, start_time, latest_time, thinking_message):
    """
    Gathers conversation history, chunks it, and uses the Flash model to summarize relevant parts,
    including filtering for essential attachments and links.
    """
    all_messages_dict = {}

    # 1. GATHER MESSAGES
    if search_scope == "CURRENT_THREAD" and thread_ts:
        try:
            result = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=100)
            for msg in result.get("messages", []):
                all_messages_dict[msg['ts']] = msg
        except Exception as e:
            logger.error(f"Error fetching thread replies for context extraction: {e}")
    
    elif search_scope in ["CURRENT_CHANNEL", "SPECIFIC_CHANNELS"]:
        channels_to_search = channel_ids if search_scope == "SPECIFIC_CHANNELS" else [channel_id]
        for c_id in channels_to_search:
            try:
                history_params = {'channel': c_id, 'limit': 1000}
                if start_time: history_params['oldest'] = str(start_time.timestamp())
                if latest_time: history_params['latest'] = str(latest_time.timestamp())
                
                history_result = client.conversations_history(**history_params)
                top_level_messages = history_result.get("messages", [])

                for msg in top_level_messages:
                    all_messages_dict[msg['ts']] = msg
                    if 'thread_ts' in msg and msg.get('reply_count', 0) > 0:
                        try:
                            logger.info(f"Found thread {msg['thread_ts']} in channel {c_id}, fetching its replies.")
                            replies_result = client.conversations_replies(channel=c_id, ts=msg['thread_ts'])
                            for reply_msg in replies_result.get("messages", []):
                                all_messages_dict[reply_msg['ts']] = reply_msg
                        except Exception as e:
                            logger.error(f"Error fetching replies for thread {msg['thread_ts']}: {e}")
            except Exception as e:
                logger.error(f"Error fetching channel history for {c_id}: {e}")

    if not all_messages_dict:
        return []

    # 2. STRUCTURE CONVERSATION FOR SUMMARIZATION
    threads = {}
    for ts, msg in all_messages_dict.items():
        thread_key = msg.get('thread_ts', ts)
        if thread_key not in threads:
            threads[thread_key] = []
        threads[thread_key].append(msg)

    for thread_key in threads:
        threads[thread_key].sort(key=lambda x: float(x['ts']))

    conversation_string = ""
    sorted_thread_keys = sorted(threads.keys(), key=float)
    for thread_key in sorted_thread_keys:
        thread_messages = threads[thread_key]
        conversation_string += f"\n--- Thread starting at {thread_key} ---"
        for msg in thread_messages:
            user_info = get_user_info(msg.get("user", "N/A"))
            user_name = user_info.get("name")
            text = msg.get("text", "")
            files = [f.get('name', 'file') for f in msg.get('files', [])]
            urls = re.findall(r'<?(https?://[\w\-./?=&%#]+)>?', text)
            
            conversation_string += f"Timestamp: {msg['ts']}\nAuthor: {user_name}\nMessage: {text}\n"
            if files:
                conversation_string += f"Files: {', '.join(files)}\n"
            if urls:
                conversation_string += f"Links: {', '.join(urls)}\n"
            conversation_string += "---"

    # 3. CHUNKING
    MAX_CHUNK_SIZE = 3500
    chunks = []
    current_chunk = ""
    for line in conversation_string.splitlines(True):
        if len(current_chunk) + len(line) > MAX_CHUNK_SIZE:
            chunks.append(current_chunk)
            current_chunk = ""
        current_chunk += line
    if current_chunk:
        chunks.append(current_chunk)

    final_structured_context = []
    
    try:
        # 4. NEW FLASH PROMPT & PROCESSING
        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                logger.info(f"Processing chunk {i+1}/{len(chunks)} for summarization.")
                try:
                    client.chat_update(channel=thinking_message['channel'], ts=thinking_message['ts'], text=f"Analyzing context... (Chunk {i+1}/{len(chunks)}) 🧐")
                except Exception as e:
                    logger.warning(f"Could not update progress message: {e}")
            else:
                logger.info("Processing single chunk for summarization.")
            flash_prompt = f"""You are a conversation summarization expert. Your task is to analyze a snippet of a Slack conversation and extract key information relevant to the user's request.

Respond with a JSON array of objects. Each object represents a single, coherent \"context unit\" and MUST have the following structure:
{{
  "summary": "A concise summary of the relevant discussion point.",
  "relevant_messages": [
    {{
      "message_id": "The timestamp of a relevant message (e.g., '1629888000.000100').",
      "files": ["A list of filenames (e.g., 'report.pdf') from this specific message that are relevant to the summary."],
      "links": ["A list of URLs from this specific message that are relevant to the summary."]
    }}
  ]
}}

If a part of the conversation is irrelevant, do not create a context unit for it. If nothing in this snippet is relevant, return an empty array [].

[User Prompt]
{user_prompt}

[Conversation Snippet]
{chunk}
"""
            flash_response = model_flash.generate_content(flash_prompt)
            
            logger.info(f"--- Raw Flash Model JSON Response (Chunk {i+1}) ---\n{flash_response.text}")
            json_match = re.search(r'```json(.*)```', flash_response.text, re.DOTALL)
            if not json_match:
                if flash_response.text.strip().startswith('['):
                    json_str = flash_response.text.strip()
                else:
                    logger.warning(f"Could not find JSON in Flash response for chunk {i+1}")
                    continue
            else:
                json_str = json_match.group(1)

            try:
                summarized_units = json.loads(json_str)
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON from Flash response for chunk {i+1}:\n{flash_response.text}")
                continue

            # 5. PROCESS THE SUMMARIZED UNITS
            for unit in summarized_units:
                summary_text = unit.get("summary", "")
                relevant_messages_data = unit.get("relevant_messages", [])

                if not summary_text or not relevant_messages_data:
                    continue

                combined_text_parts = [f"Summary of conversation: {summary_text}"]
                combined_payload_parts = []
                
                all_relevant_ids = [msg_data.get("message_id") for msg_data in relevant_messages_data if isinstance(msg_data, dict) and msg_data.get("message_id")]
                if not all_relevant_ids:
                    continue

                for msg_data in relevant_messages_data:
                    if not isinstance(msg_data, dict): continue
                    
                    msg_id = msg_data.get("message_id")
                    if not msg_id: continue

                    original_msg = all_messages_dict.get(msg_id)
                    if not original_msg: continue

                    relevant_files_for_msg = msg_data.get("files", [])
                    relevant_links_for_msg = msg_data.get("links", [])

                    if relevant_files_for_msg and "files" in original_msg:
                        file_infos_to_process = [f for f in original_msg["files"] if f.get("name") in relevant_files_for_msg]
                        for file_info in file_infos_to_process:
                            try:
                                file_name = file_info.get("name")
                                mime_type = file_info.get("mimetype", "application/octet-stream")
                                content_bytes = download_file(file_info["url_private_download"], SLACK_BOT_TOKEN)
                                
                                if mime_type.startswith(("image/", "video/")):
                                    logger.info(f"Adding relevant media file to payload: {file_name}")
                                    combined_payload_parts.append(Part.from_data(content_bytes, mime_type=mime_type))
                                    combined_text_parts.append(f"\n[Info: Including relevant attachment: {file_name}]\n")
                                    continue

                                text_content = ""
                                if mime_type == "application/pdf":
                                    text_content = extract_text_from_pdf(content_bytes)
                                elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                                    text_content = extract_text_from_docx(content_bytes)
                                elif mime_type.startswith("text/"):
                                    text_content = content_bytes.decode('utf-8', errors='ignore')

                                if text_content:
                                    summary = summarize_content(text_content, user_prompt, file_name, logger, model_flash)
                                    combined_text_parts.append(f"\n--- Summary from file ({file_name}) ---\n{summary}\n")

                            except Exception as e:
                                logger.error(f"Error processing and summarizing file {file_info.get('name')}: {e}")

                    if relevant_links_for_msg:
                        for url in relevant_links_for_msg:
                            try:
                                content = ""
                                source_name = url
                                if 'notion.so' in url or 'notion.site' in url:
                                    page_id = get_page_id_from_url(url)
                                    if page_id:
                                        content = fetch_notion_page_content(page_id)
                                elif 'slack.com' in url:
                                    combined_text_parts.append(f"\n[Info: Content from relevant Slack link is included in conversation summary: {url}]\n")
                                    continue
                                else:
                                    content = fetch_website_content(url)
                                
                                if content:
                                    summary = summarize_content(content, user_prompt, source_name, logger, model_flash)
                                    combined_text_parts.append(f"\n--- Summary from URL ({source_name}) ---\n{summary}\n")
                            except Exception as e:
                                logger.error(f"Error processing and summarizing URL {url}: {e}")

                last_ts = sorted(all_relevant_ids, key=float)[-1]
                
                final_structured_context.append({
                    "author_id": "SUMMARY_BOT",
                    "ts": last_ts,
                    "thread_ts": all_messages_dict.get(last_ts, {}).get('thread_ts'),
                    "text": "\n".join(combined_text_parts),
                    "payload": combined_payload_parts
                })

        return final_structured_context

    except Exception as e:
        logger.error(f"Error during Flash model context summarization: {e}")
        return []



# --- Slack Event Handlers ---

@app.event("app_mention")
def handle_app_mention_events(body, say, client, logger):
    event = body["event"]
    if event.get("bot_id"):
        return

    thinking_message = say(text="Thinking...", thread_ts=event.get("thread_ts") or event.get("ts"))

    def process_in_background():
        try:
            user_text = event.get("text", "").strip()
            channel_id = event["channel"]
            thread_ts = event.get("thread_ts")
            
            # Step 1: Determine if context is needed at all
            try:
                check_prompt = f"""You have the SpaceVision Technical Overview and Hardware Datasheets loaded in your system instruction.
Determine if the following user request specifically requires retrieving **past conversation history** from Slack (e.g., "what did we discuss yesterday?", "summarize the thread", "who said X?").
If the request is a general technical question or can be answered using the SpaceVision documentation you already possess, answer 'False'.
Only answer 'True' if you strictly need to read previous Slack messages to answer.

User Request: "{user_text}"
Answer ("True" or "False"):"""
                response = model_flash.generate_content(check_prompt)
                logger.info(f"Flash model 'context_needed' check response: {response.text}")
                needs_context = "true" in response.text.lower()
            except Exception as e:
                logger.error(f"Error with Flash model context_needed check: {e}")
                needs_context = True

            past_messages_structured = []
            if needs_context:
                client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="Analyzing request and checking context... 🧐")
                
                # Step 2: Get detailed search criteria
                start_time, latest_time, search_scope, channel_ids = get_search_criteria_from_prompt(user_prompt=user_text)
                
                if search_scope == "ENTIRE_WORKSPACE":
                    client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="아직 전체 채널을 대상으로 검색하는 기능은 지원하지 않습니다.\n\nSorry, searching all channels is not supported yet.")
                    return

                # Step 3: Fetch and process the context based on criteria
                past_messages_structured = extract_context_with_flash(
                    client, channel_id, thread_ts, user_text, 
                    search_scope, channel_ids, start_time, latest_time, thinking_message
                )
                logger.info(f"extract_context_with_flash found {len(past_messages_structured)} messages for context.")
                if past_messages_structured:
                    logger.info("--- Start of Summarized Context Units ---")
                    loggable_context = []
                    for unit in past_messages_structured:
                        loggable_unit = {
                            "author_id": unit.get("author_id"),
                            "ts": unit.get("ts"),
                            "thread_ts": unit.get("thread_ts"),
                            "text_summary": unit.get("text", "")[:500] + "...",
                            "payload_info": [p.mime_type for p in unit.get("payload", []) if hasattr(p, 'mime_type')]
                        }
                        loggable_context.append(loggable_unit)
                    
                    try:
                        logger.info(json.dumps(loggable_context, indent=2, ensure_ascii=False))
                    except TypeError as e:
                        logger.error(f"Could not serialize context units for logging: {e}")
                        logger.info(str(loggable_context))
                    logger.info("--- End of Summarized Context Units ---")

            client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="Generating response... 🤔")
            
            current_message_text, current_payload = process_message_for_context(event, client)
            current_message_structured = {
                "author_id": event.get("user", "N/A"),
                "ts": event.get("ts"),
                "thread_ts": event.get("thread_ts"),
                "text": current_message_text,
                "payload": current_payload
            }

            prompt_parts = []
            gemini_payload = []

            if past_messages_structured:
                prompt_parts.append("---")
                sorted_past_messages = sorted(past_messages_structured, key=lambda x: float(x['ts']))
                for msg in sorted_past_messages:
                    readable_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(msg['ts'])))
                    author_mention = f"<@{msg['author_id']}>"
                    is_reply = msg.get("thread_ts") and msg.get("ts") != msg.get("thread_ts")
                    prefix = "Reply from" if is_reply else "Message from"
                    prompt_parts.append(f"{prefix} {author_mention} ({readable_time}):\n{msg['text']}")
                    gemini_payload.extend(msg['payload'])
            
            prompt_parts.append("\n---")
            current_readable_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(current_message_structured['ts'])))
            current_author_mention = f"<@{current_message_structured['author_id']}>"
            is_current_reply = current_message_structured.get("thread_ts") and current_message_structured.get("ts") != current_message_structured.get("thread_ts")
            current_prefix = "Reply from" if is_current_reply else "Message from"
            prompt_parts.append(f"{current_prefix} {current_author_mention} ({current_readable_time}):\n{current_message_structured['text']}")
            gemini_payload.extend(current_message_structured['payload'])

            final_prompt_text = "\n\n".join(prompt_parts)
            gemini_payload.insert(0, final_prompt_text)

            if not user_text and not any(p for p in gemini_payload if not isinstance(p, str)):
                 client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="I need a question or some text to go with the files.")
                 return

            logger.info("---")
            logger.info(f"Prompt Text:\n{final_prompt_text}")
            num_parts = len([p for p in gemini_payload if not isinstance(p, str)])
            if num_parts > 0:
                logger.info(f"Additional Parts: {num_parts} (files/images/etc.)")
            logger.info("---")

            response = model_pro.generate_content(gemini_payload)
            handle_gemini_response(client, channel_id, thinking_message, response.text, thread_ts=thread_ts or event.get("ts"))

        except Exception as e:
            logger.error(f"Error in app_mention background task: {e}")
            client.chat_postMessage(text=f"An error occurred: {e}", thread_ts=event.get("ts"), channel=event.get("channel"))

    thread = threading.Thread(target=process_in_background)
    thread.start()



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