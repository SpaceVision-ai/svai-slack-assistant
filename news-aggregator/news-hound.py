import feedparser
import os
import yaml
import vertexai
from vertexai.generative_models import GenerativeModel
from dotenv import load_dotenv
import sqlite3
from datetime import datetime
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Load environment variables from .env files
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'))
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

# --- Database Setup ---
DB_FILE = 'processed_news.db'

def init_db():
    """Initializes the database and creates the table if it doesn't exist."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_articles (
                link TEXT PRIMARY KEY,
                title TEXT,
                published_date TEXT,
                processed_at TEXT
            )
        """)
        conn.commit()

def add_processed_article(link, title, published_date):
    """Adds a record of a processed article to the database."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        processed_at = datetime.now().isoformat()
        cursor.execute(
            "INSERT OR IGNORE INTO processed_articles (link, title, published_date, processed_at) VALUES (?, ?, ?, ?)",
            (link, title, published_date, processed_at)
        )
        conn.commit()

def is_article_processed(link):
    """Checks if an article has already been processed."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT link FROM processed_articles WHERE link = ?", (link,))
        return cursor.fetchone() is not None

# --- Configuration ---
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")

# --- Main Application Logic ---
def initialize_vertex_ai():
    """Initializes Vertex AI and returns the model."""
    try:
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        model = GenerativeModel("gemini-2.5-flash")
        print("Vertex AI initialized successfully.")
        return model
    except Exception as e:
        print(f"Error initializing Vertex AI: {e}")
        return None

def read_feeds_from_yaml(file_path):
    """Reads a list of RSS feed URLs from a YAML file."""
    with open(file_path, 'r') as f:
        data = yaml.safe_load(f)
        return [feed['url'] for feed in data.get('feeds', [])]

def read_context(file_path):
    """Reads the context for relevance checking from a file."""
    with open(file_path, 'r') as f:
        return f.read().strip()

def check_relevance_with_llm(model, article_title, article_summary, context):
    """Checks if an article is relevant to the given context using Vertex AI Gemini."""
    if not model:
        return False, "Vertex AI model not initialized. Skipping relevance check."
    prompt = f"""Context:
---
{context}
---
Article Title: {article_title}
Article Summary: {article_summary}
---
Is the article relevant to the context provided? If yes, please explain the connection in one sentence. Your response must be in Korean and start with \"Yes:\" or \"No:\"."""
    try:
        response = model.generate_content(prompt)
        result = response.text.strip()
        if result.startswith("Yes:"):
            return True, result[4:].strip()
        else:
            return False, ""
    except Exception as e:
        print(f"An error occurred while contacting Vertex AI: {e}")
        return False, "Error during relevance check."

def search_news(model, feeds, context):
    """Searches news from feeds and returns a list of relevant articles."""
    print("\n## Searching for relevant news...")
    all_found_articles = []
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            print(f"\n### Checking source: {feed.feed.title} ({len(feed.entries)} entries)")
            for entry in feed.entries:
                link = entry.link
                if is_article_processed(link):
                    continue
                
                title = entry.title if hasattr(entry, 'title') else ''
                summary = entry.summary if hasattr(entry, 'summary') else ''
                published = entry.get('published', 'N/A')

                is_relevant, reason = check_relevance_with_llm(model, title, summary, context)
                add_processed_article(link, title, published) # Mark as processed

                if is_relevant:
                    print(f"  -> Relevant article found: {title}")
                    article_data = {
                        'title': title,
                        'link': link,
                        'relevance_reason': reason
                    }
                    all_found_articles.append(article_data)
        except Exception as e:
            print(f"Could not parse feed {feed_url}. Error: {e}")
    return all_found_articles

def post_news_to_slack(articles, channel):
    """Posts the list of relevant articles to a Slack channel."""
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        parent_message_result = client.chat_postMessage(
            channel=channel,
            text=":newspaper: 오늘의 뉴스"
        )
        parent_ts = parent_message_result['ts']
        print(f"Successfully posted parent message to channel {channel}")

        for article in articles:
            reply_text = f"<{article['link']}|*{article['title']}*>\n{article['relevance_reason']}"
            client.chat_postMessage(
                channel=channel,
                thread_ts=parent_ts,
                text=reply_text
            )
        print(f"Successfully posted {len(articles)} articles to the thread.")

    except SlackApiError as e:
        print(f"Error posting to Slack: {e.response['error']}")

if __name__ == "__main__":
    # Initialization
    init_db()
    print("Checking configurations...")

    # 1. Check for essential environment variables
    missing_vars = []
    if not PROJECT_ID:
        missing_vars.append("GOOGLE_CLOUD_PROJECT")
    if not LOCATION:
        missing_vars.append("GOOGLE_CLOUD_LOCATION")
    if not SLACK_BOT_TOKEN:
        missing_vars.append("SLACK_BOT_TOKEN")
    if not SLACK_CHANNEL:
        missing_vars.append("SLACK_CHANNEL")

    if missing_vars:
        print(f"Error: The following environment variables are missing in your .env file: {', '.join(missing_vars)}")
        print("Exiting due to missing configuration.")
    else:
        print("All environment variables loaded successfully.")
        
        # 2. Initialize Vertex AI
        model = initialize_vertex_ai()
        if not model:
            print("Exiting because AI model failed to initialize.")
        else:
            # 3. Read feed and context files
            feeds = read_feeds_from_yaml('feeds.yaml')
            context = read_context('context.txt')
            
            if not feeds or not context:
                print("Error: feeds.yaml or context.txt file is empty. Exiting.")
            else:
                # 4. Run the main process
                found_articles = search_news(model, feeds, context)
                if found_articles:
                    post_news_to_slack(found_articles, SLACK_CHANNEL)
                else:
                    print("No new relevant news found to post to Slack.")
