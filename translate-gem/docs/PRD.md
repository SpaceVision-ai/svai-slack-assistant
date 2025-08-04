# PRD: Slack 실시간 번역 봇 (Project Mentalese)

## 1. 개요

- **프로젝트명:** Project Mentalese
- **목표:** Slack 채널의 메시지를 실시간으로 감지하여 지정된 언어(한국어 ↔ 영어)로 자동 번역하고, 그 결과를 원본 메시지의 스레드에 게시하여 다국어 사용자 간의 원활한 소통을 지원한다.
- **사용 모델:** Vertex AI `gemini-2.5-flash`

## 2. 핵심 기능 요구사항

### 2.1. 번역 채널 관리

- **채널 등록:**
    - 사용자는 슬래시 명령어 `/translate-gem add`를 통해 특정 채널을 실시간 번역 대상으로 등록할 수 있다.
    - 등록 시, 봇은 "This channel is now enabled for real-time translation."와 같은 확인 메시지를 해당 채널에 전송한다.
- **채널 삭제:**
    - 사용자는 슬래시 명령어 `/translate-gem remove`를 통해 해당 채널의 실시간 번역 기능을 중지할 수 있다.
    - 중지 시, 봇은 "Real-time translation has been disabled for this channel."와 같은 확인 메시지를 전송한다.
- **채널 목록 확인:**
    - 사용자는 슬래시 명령어 `/translate-gem list`를 통해 현재 번역이 활성화된 모든 채널의 목록을 확인할 수 있다.

### 2.2. 실시간 메시지 번역

- **대상:** 등록된 채널에 게시되는 모든 신규 메시지 및 스레드(댓글) 메시지
- **번역 규칙:**
    - Vertex AI의 Gemini 모델을 사용하여 메시지가 한국어인지 영어인지 판단하고, 반대 언어로 번역한다.
    - **프롬프트 예시 (영어로 번역 시):** `Translate the following Korean text to English: [Original Message]`
    - **프롬프트 예시 (한국어로 번역 시):** `Translate the following English text to Korean: [Original Message]`
- **결과 게시:**
    - 번역된 내용은 원본 메시지를 수정하는 대신, **원본 메시지의 스레드(댓글)에 새로운 메시지로 게시**한다.
    - 번역문 앞에는 "🌐 **Translation (EN):**" 또는 "🌐 **번역 (KR):**"과 같이 어떤 언어로 번역되었는지 명확히 표시한다.

## 3. 기술적 제약 사항 및 해결 방안

- **제약 사항:** Slack API 정책상 봇은 다른 사용자가 작성한 메시지를 직접 수정할 수 없다.
- **해결 방안:** 요구사항 2.2에 명시된 바와 같이, 번역 결과를 원본 메시지의 스레드에 댓글 형태로 게시하는 방식으로 우회하여 기능을 구현한다.

## 4. 구현 계획 (High-level)

1.  **프로젝트 설정:**
    - `translate-gemini` 디렉터리 생성
    - `main.py`, `requirements.txt`, `.env` 등 기본 파일 구성
2.  **채널 관리 기능 구현:**
    - 번역 대상 채널 목록을 저장할 JSON 파일(`registered_channels.json`) 시스템 구축
    - `/translate-gem` 슬래시 명령어 및 하위 명령어(add, remove, list) 로직 구현
3.  **실시간 번역 기능 구현:**
    - `message` 이벤트 리스너를 통해 등록된 채널의 모든 메시지 감지
    - Vertex AI (`gemini-2.5-flash`)를 연동하여 언어 감지 및 번역 수행
    - 번역 결과를 원본 메시지의 스레드에 게시하는 로직 구현
