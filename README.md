# Slack Gemini Bot

This project is a Slack bot that integrates with Google's Gemini AI through Vertex AI. It allows users to interact with the Gemini model directly from Slack for various tasks like answering questions, analyzing attached files, and summarizing conversation histories.

## Features

- **Direct Interaction**: Mention the bot in a channel or send it a Direct Message (DM) to ask questions.
- **Multimodal Analysis**: Attach files (Images, PDFs, DOCX, TXT, etc.) along with your question, and the bot will analyze their content to provide context-aware answers.
- **Conversation Summarization**: Ask the bot to summarize the recent conversation in a channel to quickly catch up on discussions.
- **Formatted Responses**: Bot responses are formatted using Slack's Block Kit for better readability.
- **Handles Long Responses**: If Gemini's response is too long for a single Slack message, it's automatically sent as a downloadable Markdown file.

## Setup

### 1. Clone the Repository
```bash
git clone <your-repository-url>
cd slack-gemini-bot
```

### 2. Create a Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Create a `.env` file by copying the example file:

```bash
cp .env.example .env
```

Now, edit the `.env` file and fill in the required credentials:

- `SLACK_BOT_TOKEN`: Your Slack bot token (starts with `xoxb-`).
- `SLACK_APP_TOKEN`: Your Slack app-level token (starts with `xapp-`).
- `GOOGLE_CLOUD_PROJECT`: Your Google Cloud Project ID.
- `GOOGLE_CLOUD_LOCATION`: The GCP region for Vertex AI (e.g., `us-central1`).

## Running the Bot

To start the bot on your local machine, run:

```bash
python main.py
```

The bot will connect to Slack using Socket Mode and will be ready for interaction.
