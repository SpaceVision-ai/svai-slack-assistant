# Migration Plan: Anthropic → OpenAI API

## 개요

`translate-gem/translate-gem.py`의 번역 엔진을 Anthropic Claude에서 OpenAI(ChatGPT) API로 교체한다.
변경 범위는 API 클라이언트 초기화, 6개의 API 호출 지점, 환경변수, 패키지 의존성에 한정된다.
로직·프롬프트·Slack/Notion 연동 코드는 변경하지 않는다.

---

## 변경 파일 목록

| 파일 | 변경 내용 |
|---|---|
| `translate-gem/translate-gem.py` | 클라이언트 교체, API 호출 패턴 변경 (6곳) |
| `translate-gem/requirements.txt` | `anthropic` → `openai` |
| `translate-gem/.env.example` | `ANTHROPIC_API_KEY` → `OPENAI_API_KEY`, 모델명 업데이트 |
| `.env.example` | 루트 공통 env에서 `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` |
| `docs/migration-anthropic-to-openai.md` | 이 문서 (작업 완료 후 삭제 또는 보관) |

---

## API 패턴 비교

### 클라이언트 초기화

```python
# Before (Anthropic)
import anthropic
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
TRANSLATION_MODEL = os.environ.get("TRANSLATION_MODEL", "claude-haiku-4-5-20251001")

# After (OpenAI)
from openai import OpenAI
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
TRANSLATION_MODEL = os.environ.get("TRANSLATION_MODEL", "gpt-4o-mini")
```

### API 호출 패턴

```python
# Before (Anthropic)
response = anthropic_client.messages.create(
    model=TRANSLATION_MODEL,
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
result = response.content[0].text.strip()

# After (OpenAI)
response = openai_client.chat.completions.create(
    model=TRANSLATION_MODEL,
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
result = response.choices[0].message.content.strip()
```

### 빈 응답 체크

```python
# Before (Anthropic)
if not response.content:
    return "[Translation failed or was blocked by safety filters]"

# After (OpenAI)
if not response.choices or not response.choices[0].message.content:
    return "[Translation failed or was blocked by safety filters]"
```

---

## 변경 지점 상세 (translate-gem.py)

### 1. import 및 클라이언트 초기화 (line 13, 38–42)

```python
# Before
import anthropic
# ...
# TRANSLATION_MODEL 환경변수로 모델 변경 가능 (기본값: claude-haiku-4-5-20251001)
# 예) TRANSLATION_MODEL=claude-sonnet-4-6 으로 설정하면 Sonnet 사용
TRANSLATION_MODEL = os.environ.get("TRANSLATION_MODEL", "claude-haiku-4-5-20251001")
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# After
from openai import OpenAI
# ...
# TRANSLATION_MODEL 환경변수로 모델 변경 가능 (기본값: gpt-4o-mini)
# 예) TRANSLATION_MODEL=gpt-4o 으로 설정하면 GPT-4o 사용
TRANSLATION_MODEL = os.environ.get("TRANSLATION_MODEL", "gpt-4o-mini")
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
```

### 2. translate_text_chunk() — line 344

```python
# Before
response = anthropic_client.messages.create(
    model=TRANSLATION_MODEL,
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
if not response.content:
    logger.warning(...)
    return "[Translation failed or was blocked by safety filters]"
return response.content[0].text.strip()

# After
response = openai_client.chat.completions.create(
    model=TRANSLATION_MODEL,
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
if not response.choices or not response.choices[0].message.content:
    logger.warning(...)
    return "[Translation failed or was blocked by safety filters]"
return response.choices[0].message.content.strip()
```

### 3. translate_text_chunks() — line 384

```python
# Before
response = anthropic_client.messages.create(
    model=TRANSLATION_MODEL,
    max_tokens=8192,
    messages=[{"role": "user", "content": prompt}]
)
if not response.content:
    logger.warning(...)
    return ["[Translation failed or was blocked by safety filters]"] * len(texts)
response_text = response.content[0].text

# After
response = openai_client.chat.completions.create(
    model=TRANSLATION_MODEL,
    max_tokens=8192,
    messages=[{"role": "user", "content": prompt}]
)
if not response.choices or not response.choices[0].message.content:
    logger.warning(...)
    return ["[Translation failed or was blocked by safety filters]"] * len(texts)
response_text = response.choices[0].message.content
```

### 4. process_notion_translation() — line 522

```python
# Before
english_title = anthropic_client.messages.create(
    model=TRANSLATION_MODEL,
    max_tokens=256,
    messages=[{"role": "user", "content": prompt}]
).content[0].text.strip().replace('"', '')

# After
english_title = openai_client.chat.completions.create(
    model=TRANSLATION_MODEL,
    max_tokens=256,
    messages=[{"role": "user", "content": prompt}]
).choices[0].message.content.strip().replace('"', '')
```

### 5. create_url_summary_blocks() — line 662

```python
# Before
combined_response = anthropic_client.messages.create(
    model=TRANSLATION_MODEL,
    max_tokens=1024,
    messages=[{"role": "user", "content": summarize_and_translate_prompt}]
).content[0].text.strip()

# After
combined_response = openai_client.chat.completions.create(
    model=TRANSLATION_MODEL,
    max_tokens=1024,
    messages=[{"role": "user", "content": summarize_and_translate_prompt}]
).choices[0].message.content.strip()
```

### 6. translate_message() 메인 번역 — line 778

```python
# Before
translation_response = anthropic_client.messages.create(
    model=TRANSLATION_MODEL,
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
translated_text_with_placeholders = translation_response.content[0].text.strip()

# After
translation_response = openai_client.chat.completions.create(
    model=TRANSLATION_MODEL,
    max_tokens=2048,
    messages=[{"role": "user", "content": prompt}]
)
translated_text_with_placeholders = translation_response.choices[0].message.content.strip()
```

### 7. translate_message() Notion 제목 번역 — line 820

```python
# Before
title_translation_response = anthropic_client.messages.create(
    model=TRANSLATION_MODEL,
    max_tokens=256,
    messages=[{"role": "user", "content": prompt}]
)
english_title = title_translation_response.content[0].text.strip()

# After
title_translation_response = openai_client.chat.completions.create(
    model=TRANSLATION_MODEL,
    max_tokens=256,
    messages=[{"role": "user", "content": prompt}]
)
english_title = title_translation_response.choices[0].message.content.strip()
```

---

## 환경변수 변경

### `.env` (로컬 실제 파일 — 직접 수정)

```env
# Before
ANTHROPIC_API_KEY="sk-ant-..."

# After
OPENAI_API_KEY="sk-..."
```

### `.env.example` (루트)

```env
# Before
ANTHROPIC_API_KEY="sk-ant-..."

# After
OPENAI_API_KEY="sk-..."
```

### `translate-gem/.env.example`

```env
# Before
# Anthropic API Key
ANTHROPIC_API_KEY="sk-ant-..."

# After
# OpenAI API Key
OPENAI_API_KEY="sk-..."
```

---

## requirements.txt 변경

```text
# Before
anthropic

# After
openai
```

패키지 업데이트:
```bash
pip uninstall anthropic -y
pip install openai
```

---

## 작업 순서

1. `translate-gem/translate-gem.py` — import/클라이언트 초기화 변경
2. `translate-gem/translate-gem.py` — 6개 API 호출 지점 변경 (위 순서대로)
3. `translate-gem/requirements.txt` — `anthropic` → `openai`
4. `.env` (로컬) — `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` 값 교체
5. `.env.example` (루트) — 키명 업데이트
6. `translate-gem/.env.example` — 키명 업데이트
7. `.venv`에 패키지 반영: `pip install -r translate-gem/requirements.txt`
8. 봇 재시작 후 동작 확인
9. 커밋 & push

---

## 모델 선택 가이드

| 용도 | Anthropic (현재) | OpenAI (권장) |
|---|---|---|
| 기본 번역 | `claude-haiku-4-5-20251001` | `gpt-4o-mini` |
| 고품질 번역 | `claude-sonnet-4-6` | `gpt-4o` |

`gpt-4o-mini`는 속도·비용 면에서 Claude Haiku와 유사한 포지션이다.

---

## 주의사항

- `translate-gem/.env`의 `ANTHROPIC_API_KEY`도 `OPENAI_API_KEY`로 교체해야 한다 (git 미추적 파일).
- 루트 `.env`의 `ANTHROPIC_API_KEY`도 동일하게 교체.
- `TRANSLATION_MODEL` 환경변수를 기존에 설정해둔 경우 OpenAI 모델명으로 변경 필요.
- systemd 서비스 재시작: `sudo systemctl restart translate-gem`
