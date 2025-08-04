# Slack Bot 설정 가이드

이 문서는 슬랙 봇, 특히 Translate-Gem과 같은 다이렉트 메시지(DM) 기능을 사용하는 봇을 설정할 때 필요한 주요 권한(Scopes) 및 구성에 대해 설명합니다.

## 1. OAuth & Permissions

[Slack API > Your App > OAuth & Permissions](https://api.slack.com/apps) 페이지에서 다음 권한을 **Bot Token Scopes**에 추가해야 합니다.

- `chat:write`: 봇이 채널에 메시지를 보낼 수 있도록 허용합니다.
- `im:write`: 봇이 사용자에게 다이렉트 메시지(DM)를 보낼 수 있도록 허용합니다. **(DM 기능의 핵심 권한)**
- `im:history`: 봇이 다이렉트 메시지 채널의 대화 내역을 읽을 수 있도록 허용합니다.

## 2. App Home

[Slack API > Your App > App Home](https://api.slack.com/apps) 페이지에서 사용자와 봇의 상호작용을 활성화해야 합니다.

- **Messages Tab**을 활성화합니다.
- **"Allow users to send Slash commands and messages from the messages tab"** 옵션을 반드시 체크해야 합니다. 이 설정이 꺼져 있으면 사용자가 봇에게 DM을 보낼 수 없습니다.

## 3. Event Subscriptions

[Slack API > Your App > Event Subscriptions](https://api.slack.com/apps) 페이지에서 봇이 메시지 이벤트를 수신할 수 있도록 설정해야 합니다.

- **Enable Events**를 활성화합니다.
- **Subscribe to bot events** 섹션에서 `message.im` 이벤트를 추가합니다. 이 이벤트는 사용자가 봇에게 DM을 보낼 때 발생하는 이벤트입니다.

---

위 설정들을 올바르게 구성하고 앱을 워크스페이스에 다시 설치하면, 봇이 정상적으로 다이렉트 메시지를 주고받을 수 있습니다.
