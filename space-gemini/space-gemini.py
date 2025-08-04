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

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv(dotenv_path="../.env")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")

# Initializes your app with your bot token and socket mode handler
app = App(token=SLACK_BOT_TOKEN)

# Initialize Vertex AI
vertexai.init(project=os.environ.get("GOOGLE_CLOUD_PROJECT"), location=os.environ.get("GOOGLE_CLOUD_LOCATION"))

# Use a model that supports multimodal inputs
model = GenerativeModel("gemini-2.5-pro")

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

def handle_gemini_response(app, channel_id, thinking_message, text, thread_ts=None):
    SLACK_MSG_LIMIT = 4000
    thinking_ts = thinking_message['ts']
    try:
        if len(text) <= SLACK_MSG_LIMIT:
            # For shorter messages, use Block Kit for rich formatting.
            # The text field in a "section" block supports mrkdwn.
            blocks = [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    # Ensure the text is not empty, which can cause an error.
                    "text": text or "(empty response)"
                }
            }]
            app.client.chat_update(
                channel=channel_id,
                ts=thinking_ts,
                blocks=blocks,
                # Provide a plain-text summary for notifications
                text="Gemini AI가 답변을 생성했습니다."
            )
        else:
            logger.info(f"Response is too long ({len(text)} chars). Creating a file.")
            file_path = f"gemini_response_{int(time.time())}.md"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                app.client.chat_delete(channel=channel_id, ts=thinking_ts)
                app.client.files_upload_v2(
                    channel=channel_id, file=file_path, title="Gemini AI Response",
                    initial_comment="답변이 길어서 파일로 첨부해 드렸어요. 📄", thread_ts=thread_ts
                )
            finally:
                os.remove(file_path)
    except Exception as e:
        logger.error(f"Error in handle_gemini_response: {e}")
        app.client.chat_postMessage(text=f"An error occurred: {e}", thread_ts=thread_ts, channel=channel_id)

# --- Main Event Processing Logic ---

def format_conversation_history(history):
    messages = history.get("messages", [])
    formatted_lines = []
    # Process messages in reverse order to have the oldest first
    for msg in reversed(messages):
        # Skip bot messages or messages with no user
        if msg.get("bot_id") or not msg.get("user"):
            continue
        user_name = get_user_info(msg["user"])
        text = msg.get("text", "")
        formatted_lines.append(f"{user_name}: {text}")
    return "\n".join(formatted_lines)

def process_event(event, say):
    # This function now handles simple Q&A and file processing
    # The history summarization is handled in the app_mention handler directly
    user_text = event.get("text", "")
    channel_id = event["channel"]
    thread_ts = event.get("ts")
    files = event.get("files", [])

    if not user_text and not files:
        return

    thinking_message = say(text="Thinking...", thread_ts=thread_ts, channel=channel_id)
    gemini_payload = []
    extracted_texts = []

    if files:
        for file_info in files:
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
                app.client.chat_update(channel=channel_id, ts=thinking_message["ts"], text=f"Sorry, I couldn't process the file: {file_info.get('name')}.")
                return

    full_prompt_text = "\n".join([user_text] + extracted_texts).strip()
    if not full_prompt_text and files:
        full_prompt_text = "Please describe, summarize, or analyze the contents of the attached file(s)."
    
    gemini_payload.insert(0, full_prompt_text)
    if not gemini_payload[0]:
        app.client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="I need a question or some text to go with the files.")
        return

    response = model.generate_content(gemini_payload)
    handle_gemini_response(app, channel_id, thinking_message, response.text, thread_ts=thread_ts)

# --- Slack Event Handlers ---

@app.event("app_mention")
def handle_app_mention_events(body, say, logger):
    logger.info(body)
    try:
        event = body["event"]
        user_text = event.get("text", "").lower()
        channel_id = event["channel"]
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        HELP_KEYWORDS = ["뭐 할 수 있어", "도와줘", "help", "기능", "what can you do"]
        is_help_request = any(keyword in user_text for keyword in HELP_KEYWORDS)

        if is_help_request:
            help_text = (
                "안녕하세요! 저는 Gemini AI 봇입니다. 제가 할 수 있는 일은 다음과 같아요. 🤖\n\n"
                "🔹 **질문 답변**\n"
                "   - 저를 멘션하고 무엇이든 질문하시면 답변해 드려요.\n\n"
                "🔹 **파일 처리**\n"
                "   - 이미지, PDF, DOCX, 텍스트 파일을 첨부하고 질문하시면 내용을 분석하고 답변해 드려요.\n"
                "   - 예: `이 문서 요약해 줘`, `이 이미지에 대해 설명해 줘`\n\n"
                "🔹 **대화 요약**\n"
                "   - **스레드 요약:** 특정 메시지의 스레드(댓글) 안에서 저를 멘션하고 `요약` 또는 `정리`라고 요청하시면 해당 스레드의 대화 내용을 요약해 드려요.\n"
                "   - **채널 요약:** 채널에서 저를 멘션하고 `요약` 또는 `정리`라고 요청하시면 최근 채널 대화 내용을 요약해 드려요.\n\n"
                "🔹 **DM (개인 메시지)**\n"
                "   - 저와의 DM에서는 멘션 없이 바로 대화하거나 파일을 처리할 수 있어요.\n\n"
                "궁금한 점이 있다면 언제든지 물어보세요!"
            )
            say(text=help_text, thread_ts=message_ts, channel=channel_id)
            return

        SUMMARY_KEYWORDS = ["요약", "정리", "summarize", "recap"]
        is_summary_request = any(keyword in user_text for keyword in SUMMARY_KEYWORDS)

        if is_summary_request:
            user_id = event["user"]
            user_name = get_user_info(user_id)
            # --- THREAD SUMMARIZATION LOGIC ---
            if thread_ts:
                thinking_message = say(text=f"네, {user_name}님. 요청하신 스레드 댓글들을 읽고 요약하는 중입니다... 🧐", thread_ts=thread_ts, channel=channel_id)
                try:
                    # Fetch up to 151 replies to check if there are more than 150
                    history_response = app.client.conversations_replies(channel=channel_id, ts=thread_ts, limit=151)
                    messages = history_response.get("messages", [])

                    if len(messages) > 150:
                        # Notify the user that the thread is too long and will be truncated
                        app.client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=thread_ts,
                            text="⚠️ 스레드 댓글이 150개를 초과하여, 가장 최근 150개만 요약에 포함됩니다."
                        )
                        # Truncate the messages to the most recent 150
                        history_response["messages"] = messages[:150]

                    formatted_history = format_conversation_history(history_response)
                    
                    if not formatted_history:
                        app.client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="요약할 댓글을 찾지 못했어요. 😢")
                        return

                    summary_prompt = (
                        f"다음 Slack 스레드 대화 내용을 요약해 주세요. "
                        f"다른 부가적인 설명 없이 요약된 내용만 제공해야 합니다.\n\n"
                        f"--- 사용자의 요청사항 ---\n{event.get('text')}\n\n"
                        f"--- 스레드 댓글 내용 ---\n{formatted_history}"
                    )
                    response = model.generate_content(summary_prompt)
                    handle_gemini_response(app, channel_id, thinking_message, response.text, thread_ts=thread_ts)

                except Exception as e:
                    logger.error(f"Error during thread summarization: {e}")
                    app.client.chat_update(channel=channel_id, ts=thinking_message["ts"], text=f"죄송합니다, 스레드 댓글을 가져오는 중 오류가 발생했어요: {e}")
            
            # --- CHANNEL SUMMARIZATION LOGIC ---
            else:
                thinking_message = say(text="채널 대화 내용을 읽고 요약하는 중입니다... 🧐", thread_ts=message_ts, channel=channel_id)
                try:
                    history_response = app.client.conversations_history(channel=channel_id, limit=100)
                    formatted_history = format_conversation_history(history_response)

                    if not formatted_history:
                        app.client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="요약할 대화 내용을 찾지 못했어요. 😢")
                        return

                    summary_prompt = (
                        f"다음 Slack 채널의 대화 내용을 요약해 주세요. "
                        f"다른 부가적인 설명 없이 요약된 내용만 제공해야 합니다.\n\n"
                        f"--- 사용자의 요청사항 ---\n{event.get('text')}\n\n"
                        f"--- 채널 대화 내용 ---\n{formatted_history}"
                    )
                    response = model.generate_content(summary_prompt)
                    handle_gemini_response(app, channel_id, thinking_message, response.text, thread_ts=message_ts)

                except Exception as e:
                    logger.error(f"Error during channel summarization: {e}")
                    app.client.chat_update(channel=channel_id, ts=thinking_message["ts"], text=f"죄송합니다, 채널 대화 내용을 가져오는 중 오류가 발생했어요: {e}")
        
        # --- DEFAULT Q&A LOGIC ---
        else:
            process_event(event, say)

    except Exception as e:
        logger.error(f"Error in app_mention handler: {e}")
        event = body.get("event", {})
        say(text=f"An error occurred: {e}", thread_ts=event.get("ts"), channel=event.get("channel"))


@app.event("message")
def handle_direct_messages(body, say, logger):
    # DM handler remains for simple Q&A and file attachments in DMs
    logger.info(body)
    try:
        event = body["event"]
        if event.get("bot_id") or (event.get("subtype") and event.get("subtype") != "file_share"):
            return
        if event.get("channel_type") == "im":
            process_event(event, say)
    except Exception as e:
        logger.error(f"Error handling direct message: {e}")
        event = body.get("event", {})
        say(text=f"An error occurred: {e}", channel=event.get("channel"))

if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()