import feedparser
import os
import yaml
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from dotenv import load_dotenv
import sqlite3
from datetime import datetime

# Load environment variables from .env files in parent and local directory.
# The local .env file will override the parent .env file.
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
# The script now reads GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION from the .env file.
# Make sure to authenticate with Google Cloud CLI:
# gcloud auth application-default login

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")

# Initialize Vertex AI
try:
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.5-flash")
    print("Vertex AI initialized successfully.")
except Exception as e:
    print(f"Error initializing Vertex AI: {e}")
    model = None

def read_feeds_from_yaml(file_path):
    """Reads a list of RSS feed URLs from a YAML file."""
    with open(file_path, 'r') as f:
        data = yaml.safe_load(f)
        return [feed['url'] for feed in data.get('feeds', [])]

def read_context(file_path):
    """Reads the context for relevance checking from a file."""
    with open(file_path, 'r') as f:
        return f.read().strip()

def check_relevance_with_llm(article_title, article_summary, context):
    """
    Checks if an article is relevant to the given context using Vertex AI Gemini.
    Returns a tuple: (is_relevant, reason).
    """
    if not model:
        return False, "Vertex AI model not initialized. Skipping relevance check."

    prompt = f"""
    Context:
    ---
    {context}
    ---
    Article Title: {article_title}
    Article Summary: {article_summary}
    ---
    Is the article relevant to the context provided?
    If yes, please explain the connection in one sentence.
    Your response must be in Korean and start with "Yes:" or "No:".
    """

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

def search_news(feeds, context):
    """
    Searches news from feeds and prints articles relevant to the context.
    """
    print(f"\n## Checking news for relevance against context...")
    print("-" * 40)

    all_found_articles = []

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            source_name = feed.feed.title
            print(f"\n### Checking source: {source_name}")
            print(f"  -> Found {len(feed.entries)} entries in the feed.")

            for entry in feed.entries:
                title = entry.title if hasattr(entry, 'title') else ''
                summary = entry.summary if hasattr(entry, 'summary') else ''
                link = entry.link
                published = entry.get('published', 'N/A')

                if is_article_processed(link):
                    print(f"  -> Skipping already processed article: {title}")
                    continue

                is_relevant, reason = check_relevance_with_llm(title, summary, context)
                
                # Mark article as processed regardless of relevance
                add_processed_article(link, title, published)

                if is_relevant:
                    article_data = {
                        'title': title,
                        'link': link,
                        'published': published,
                        'source': source_name,
                        'relevance_reason': reason
                    }
                    if link not in [a['link'] for a in all_found_articles]:
                        all_found_articles.append(article_data)
                        print(f"  -> Relevant article found: {title}\n     reason: {reason}")

        except Exception as e:
            print(f"Could not parse feed {feed_url}. Error: {e}")


    print("\n" + "="*40)
    print("            RELEVANT NEWS REPORT")
    print("="*40 + "\n")

    if all_found_articles:
        for article in all_found_articles:
            print(f"- **{article['title']}**")
            print(f"  - Source: {article['source']}")
            print(f"  - Link: {article['link']}")
            print(f"  - Published: {article['published']}")
            print(f"  - Relevance: {article['relevance_reason']}")
            print("-" * 20)
    else:
        print("  -> No relevant news found across all feeds.")


if __name__ == "__main__":
    init_db()
    if not PROJECT_ID or not LOCATION:
        print("Error: GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION environment variables must be set.")
        print("Please create a .env file with these values or set them in your shell.")
    else:
        feeds = read_feeds_from_yaml('news-aggregator/feeds.yaml')
        context = read_context('news-aggregator/context.txt')
        if not feeds:
            print("No feeds found in feeds.yaml. Please add some RSS feed URLs.")
        elif not context:
            print("Context is empty. Please add content to context.txt.")
        else:
            search_news(feeds, context)
