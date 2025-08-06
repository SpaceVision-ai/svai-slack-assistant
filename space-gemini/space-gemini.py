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

# .env 파일 (봇 특정 및 공통) 로드
load_dotenv() # 현재 디렉터리의 .env 로드
load_dotenv(dotenv_path="../.env") # 상위 디렉터리의 .env 로드

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

def format_conversation_history(client, channel_id, messages):
    if not messages:
        return ""
    
    formatted_messages = []
    # The history is returned newest-first, so we reverse it for chronological order.
    for msg in reversed(messages):
        # Skip non-user messages or messages without text
        if "user" not in msg or "text" not in msg:
            continue
        
        user_id = msg["user"]
        user_name = get_user_info(user_id)
        text = msg["text"]
        
        # Clean up mentions from the text
        text = re.sub(r'<@U[A-Z0-9]+>', '', text).strip()
        
        if text: # Only add messages that have content after cleaning
            formatted_messages.append(f"{user_name}: {text}")

        # If the message has replies, fetch the thread
        if msg.get("reply_count", 0) > 0:
            thread_ts = msg.get("thread_ts", msg.get("ts"))
            try:
                replies_response = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
                # Skip the parent message (already added) and format replies
                for reply in replies_response.get("messages", [])[1:]:
                    if "user" in reply and "text" in reply:
                        reply_user_name = get_user_info(reply["user"])
                        reply_text = re.sub(r'<@U[A-Z0-9]+>', '', reply["text"]).strip()
                        if reply_text:
                            formatted_messages.append(f"  (in thread) {reply_user_name}: {reply_text}")
            except Exception as e:
                logger.error(f"Error fetching replies for thread {thread_ts}: {e}")

            
    return "\n".join(formatted_messages)

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

def handle_gemini_response(client, channel_id, thinking_message, text, thread_ts=None):
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
            client.chat_update(
                channel=channel_id,
                ts=thinking_ts,
                blocks=blocks,
                # Provide a plain-text summary for notifications
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

        # --- 스레드 내 대화 (컨텍스트 유지) ---
        if thread_ts:
            thinking_message = say(text="Thinking...", thread_ts=thread_ts, channel=channel_id)
            try:
                history_response = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
                messages = history_response.get("messages", [])
                conversation_history = format_conversation_history(client, channel_id, messages)
                
                prompt = (
                    f"You are a helpful AI assistant. Continue the following Slack conversation, paying close attention to the context. "
                    f"The user is asking a follow-up question in a thread.\n\n"
                    f"--- Conversation History ---\n{conversation_history}\n\n"
                    f"--- New Question ---\n{user_text}\n\n"
                    f"Please provide a direct answer to the new question based on the history."
                )

                gemini_payload = [prompt]
                files = event.get("files", [])
                if files:
                    # (파일 처리 로직은 여기에 추가될 수 있습니다. 지금은 텍스트에 집중합니다.)
                    pass

                response = model.generate_content(gemini_payload)
                handle_gemini_response(client, channel_id, thinking_message, response.text, thread_ts=thread_ts)

            except Exception as e:
                logger.error(f"Error processing in-thread mention: {e}")
                say(text=f"An error occurred while processing the conversation: {e}", thread_ts=thread_ts, channel=channel_id)
            return

        # --- 새로운 대화 시작 (일반 채널 멘션) ---
        # (기존의 요약, 도움말, 파일 처리 로직은 여기에 위치합니다)
        HELP_KEYWORDS = ["뭐 할 수 있어", "도와줘", "help", "기능", "what can you do"]
        is_help_request = any(keyword in user_text.lower() for keyword in HELP_KEYWORDS)

        if is_help_request:
            help_text = (
                "Hello! I'm a Gemini AI bot. Here's what I can do for you: 🤖\n\n"
                "🔹 **Question & Answer**\n"
                "   - Mention me and ask anything, and I'll provide an answer.\n\n"
                "🔹 **File Processing**\n"
                "   - Attach images, PDFs, DOCX, or text files and ask a question. I'll analyze the content and respond.\n"
                "   - E.g., `Summarize this document`, `Describe this image`\n\n"
                "🔹 **Conversation Summaries**\n"
                "   - **Thread Summary:** Mention me in a thread and ask me to `summarize` or `recap`, and I'll summarize the conversation in that thread.\n"
                "   - **Channel Summary:** Mention me in a channel and ask me to `summarize` or `recap`, and I'll summarize the recent channel conversation.\n\n"
                "🔹 **Direct Messages (DM)**\n"
                "   - In a DM with me, you can chat or process files directly without a mention.\n\n"
                "Feel free to ask if you have any questions!"
            )
            say(text=help_text, thread_ts=message_ts, channel=channel_id)
            return

        SUMMARY_KEYWORDS = ["요약", "정리", "summarize", "recap"]
        is_summary_request = any(keyword in user_text.lower() for keyword in SUMMARY_KEYWORDS)

        if is_summary_request:
            user_id = event["user"]
            user_name = get_user_info(user_id)
            # --- THREAD SUMMARIZATION LOGIC (This is different from conversation context) ---
            if event.get("thread_ts"): # 요약 요청이 스레드 안에서 일어났을 때
                summary_thread_ts = event.get("thread_ts")
                thinking_message = say(text=f"Okay, {user_name}. I'm reading the thread and preparing a summary... 🧐", thread_ts=summary_thread_ts, channel=channel_id)
                try:
                    history_response = client.conversations_replies(channel=channel_id, ts=summary_thread_ts, limit=301)
                    # (이하 요약 로직은 기존과 동일하게 유지)
                    messages = history_response.get("messages", [])
                    if len(messages) > 300:
                        client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=summary_thread_ts,
                            text="⚠️ The thread has more than 300 replies. The summary will only include the most recent 300."
                        )
                        history_response["messages"] = messages[:300]
                    formatted_history = format_conversation_history(history_response)
                    if not formatted_history:
                        client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="요약할 댓글을 찾지 못했어요. 😢")
                        return
                    summary_prompt = (
                        f"다음 Slack 스레드 대화 내용을 요약해 주세요. "
                        f"다른 부가적인 설명 없이 요약된 내용만 제공해야 합니다.\n\n"
                        f"--- 사용자의 요청사항 ---\n{event.get('text')}\n\n"
                        f"--- 스레드 댓글 내용 ---\n{formatted_history}"
                    )
                    response = model.generate_content(summary_prompt)
                    handle_gemini_response(client, channel_id, thinking_message, response.text, thread_ts=summary_thread_ts)
                except Exception as e:
                    logger.error(f"Error during thread summarization: {e}")
                    client.chat_update(channel=channel_id, ts=thinking_message["ts"], text=f"죄송합니다, 스레드 댓글을 가져오는 중 오류가 발생했어요: {e}")
            
            # --- CHANNEL SUMMARIZATION LOGIC ---
            else:
                thinking_message = say(text="채널 대화 내용을 읽고 요약하는 중입니다... 🧐", thread_ts=message_ts, channel=channel_id)
                try:
                    history_response = client.conversations_history(channel=channel_id, limit=100)
                    messages = history_response.get("messages", [])
                    formatted_history = format_conversation_history(client, channel_id, messages)
                    if not formatted_history:
                        client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="요약할 대화 내용을 찾지 못했어요. 😢")
                        return
                    summary_prompt = (
                        f"다음 Slack 채널의 대화 내용을 요약해 주세요. "
                        f"다른 부가적인 설명 없이 요약된 내용만 제공해야 합니다.\n\n"
                        f"--- 사용자의 요청사항 ---\n{event.get('text')}\n\n"
                        f"--- 채널 대화 내용 ---\n{formatted_history}"
                    )
                    response = model.generate_content(summary_prompt)
                    handle_gemini_response(client, channel_id, thinking_message, response.text, thread_ts=message_ts)
                except Exception as e:
                    logger.error(f"Error during channel summarization: {e}")
                    client.chat_update(channel=channel_id, ts=thinking_message["ts"], text=f"죄송합니다, 채널 대화 내용을 가져오는 중 오류가 발생했어요: {e}")
        
        # --- DEFAULT Q&A LOGIC (New Conversation) ---
        else:
            thinking_message = say(text="Thinking...", thread_ts=message_ts, channel=channel_id)
            gemini_payload = []
            extracted_texts = []
            files = event.get("files", [])

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
                        client.chat_update(channel=channel_id, ts=thinking_message["ts"], text=f"Sorry, I couldn't process the file: {file_info.get('name')}.")
                        return

            full_prompt_text = "\n".join([user_text] + extracted_texts).strip()
            if not full_prompt_text and files:
                full_prompt_text = "Please describe, summarize, or analyze the contents of the attached file(s)."
            
            gemini_payload.insert(0, full_prompt_text)
            if not gemini_payload[0]:
                client.chat_update(channel=channel_id, ts=thinking_message["ts"], text="I need a question or some text to go with the files.")
                return

            response = model.generate_content(gemini_payload)
            handle_gemini_response(client, channel_id, thinking_message, response.text, thread_ts=message_ts)

    except Exception as e:
        logger.error(f"Error in app_mention handler: {e}")
        event = body.get("event", {})
        say(text=f"An error occurred: {e}", thread_ts=event.get("ts"), channel=event.get("channel"))


@app.event("message")
def handle_direct_messages(body, say, client, logger):
    # DM handler now re-uses the app_mention logic for consistency
    logger.info(body)
    try:
        event = body["event"]
        if event.get("bot_id") or (event.get("subtype") and event.get("subtype") != "file_share"):
            return
        if event.get("channel_type") == "im":
            # To reuse the mention handler, we can treat the DM as a mention.
            # The logic inside handle_app_mention_events will process it correctly.
            handle_app_mention_events(body, say, client, logger)
    except Exception as e:
        logger.error(f"Error handling direct message: {e}")
        event = body.get("event", {})
        say(text=f"An error occurred: {e}", channel=event.get("channel"))

if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
