# Translate-Gem User Guide

Translate-Gem is a bot that provides real-time message translation in Slack channels and direct messages. It helps facilitate smooth communication by automatically translating Korean into English and all other languages into Korean.

## 1. Basic Translation Features

- **Automatic Language Detection and Translation**: Korean messages are translated into English, and all other languages (e.g., English, Japanese) are translated into Korean.
- **Translation Progress Indicator**: Since the bot uses Google's `gemini-2.5-flash` model internally, there may be a short delay of a few seconds for translation. A waiting message will be displayed while the translation is in progress.
- **Clear Author Attribution**: The translated message always mentions the original author, so it's clear who wrote the message.
- **Response Method by Channel/DM Type**:
    - **Public/Private Channels**: The translation is posted as a reply in the original message's thread.
    - **1:1 and Group DMs**: The translation is sent as a regular message in the conversation.

## 2. Usage in Channels

You can enable or disable real-time translation in channels where the bot has been invited using specific commands.

- **Enable Translation**: ` /translate-gem-channel add `
  - All messages in the channel will now be translated in real-time.
- **Disable Translation**: ` /translate-gem-channel remove `
  - Real-time translation will be stopped.
- **List Active Channels**: ` /translate-gem-channel list `
  - Shows a list of all channels where translation is currently active.

## 3. Usage in Direct Messages (DMs)

- **1:1 DM**: In a one-on-one conversation with the bot, all messages are automatically translated without any setup.
- **Group DM**: When you invite the bot to a group DM with multiple people, the automatic translation feature is activated immediately.

## 4. Notion Integration: Document Title Translation

This feature helps standardize Notion document titles into English, following company policy.

- **Feature Description**: When a Notion document link is shared in a channel or DM, if the document title is in Korean, the bot will automatically suggest an English translation.
- **How It Works**:
    1. When the bot detects a Korean title, it will display a button in the thread suggesting a new title in the format **`Original Korean Title (Translated English Title)`**.
    2. Clicking the **`✅ Yes, change it`** button will immediately update the Notion document's title to the suggested format.
    3. If you don't want to change it, you can close the suggestion by clicking the **`No`** button.
- **Permission Error Guide**: If the bot does not have access to the Notion page, the following message will be displayed. In this case, you can grant access by inviting the **`Translate Gem`** bot via the **`Share`** button at the top of the Notion page.
    > :warning: I can't access that Notion page. It might not exist, or I may not have permission.
    >
    > :warning: Notion 페이지에 접근할 수 없습니다. 페이지가 존재하지 않거나, 저에게 접근 권한이 없는 것 같아요.

## 5. Future Plans

- **Full Notion Document Translation**: While only title translation is currently supported, a feature to translate the entire body of a Notion document and create a new page will be added in the future.

---

If you have any questions, please feel free to ask!
