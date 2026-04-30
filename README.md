# SVAI Slack Assistant

SpaceVision AI의 Slack 봇 모음입니다. 현재 운영 중인 서비스는 **Translate Gem**입니다.

## 프로젝트 구조

```
svai-slack-assistant/
├── translate-gem/
│   ├── translate-gem.py        # 실시간 번역 봇
│   ├── translate-gem.service   # systemd 서비스 파일
│   ├── requirements.txt
│   └── docs/                   # 기능 명세 및 사용 가이드
├── docs/                       # 인프라 및 봇 설정 가이드
└── .venv/                      # 공용 가상환경
```

> `slack-space-gemini/`, `news-aggregator/`는 별도로 관리되며 git 추적에서 제외됩니다.

---

## Translate Gem

등록된 Slack 채널의 메시지를 **한국어 ↔ 영어** 자동 번역하여 원본 메시지의 스레드에 게시합니다.  
Anthropic Claude(`claude-haiku-4-5-20251001` 기본값)를 번역 엔진으로 사용합니다.

### 주요 기능

- **실시간 번역**: 등록 채널의 메시지를 감지해 반대 언어로 번역 후 스레드에 게시
- **Notion 페이지 번역**: 채널에 공유된 Notion URL을 감지하여 한국어 제목 페이지를 영어로 번역 제안
- **URL 요약**: 일반 URL 공유 시 페이지 내용을 원문 요약 + 번역 요약으로 스레드에 게시 (병렬 처리)
- **슬래시 커맨드**:
  - `/translate-gem-channel add/remove/list` — 채널 번역 활성화/비활성화/목록 조회
  - `/translate-notion` — Notion 페이지 전체 한→영 번역
  - `/translate-notion-jp` — Notion 페이지 전체 한→일 번역

### 설정

#### 1. 가상환경 및 의존성 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r translate-gem/requirements.txt
```

#### 2. 환경변수 설정

`translate-gem/.env` 파일 생성:

```env
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
ANTHROPIC_API_KEY=sk-ant-...
NOTION_API_KEY=ntn_...
```

번역 모델 변경이 필요한 경우 루트 `.env` 또는 시스템 환경변수에 추가:

```env
TRANSLATION_MODEL=claude-sonnet-4-6
```

#### 3. 실행

**직접 실행:**
```bash
cd translate-gem
python translate-gem.py
```

**systemd 서비스로 실행 (운영):**
```bash
sudo cp translate-gem/translate-gem.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable translate-gem
sudo systemctl start translate-gem
```

### Slack 앱 설정

봇 설정 방법은 [`docs/slack-bot-setting-guide.md`](docs/slack-bot-setting-guide.md)를 참고하세요.

---

## 참고 문서

- [`docs/slack-bot-setting-guide.md`](docs/slack-bot-setting-guide.md) — Slack OAuth 권한 및 Event Subscriptions 설정
- [`docs/gemini-automated-code-assistant-guide.md`](docs/gemini-automated-code-assistant-guide.md) — GCP VM 기반 GitHub Actions 자동 코드 리뷰 구축 가이드
- [`translate-gem/docs/PRD.md`](translate-gem/docs/PRD.md) — 번역 봇 기능 명세
- [`translate-gem/docs/user_guide_KR.md`](translate-gem/docs/user_guide_KR.md) — 사용자 가이드 (한국어)
- [`translate-gem/docs/user_guide_EN.md`](translate-gem/docs/user_guide_EN.md) — 사용자 가이드 (English)
