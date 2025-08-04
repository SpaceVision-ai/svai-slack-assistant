import os
import sys
import argparse
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import notion_client

# Vertex AI SDK 임포트
import vertexai
from vertexai.generative_models import GenerativeModel

# --- 설정 ---
# .config 파일에서 환경 변수 로드
project_root = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(project_root, '.config')

if os.path.exists(config_path):
    load_dotenv(dotenv_path=config_path)
else:
    print(f"경고: 설정 파일(.config)을 찾을 수 없습니다. 경로: {config_path}")

# API 키 및 GCP 환경 변수 설정
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")

# 필수 환경 변수 확인
if not NOTION_API_KEY:
    print("오류: .config 파일 또는 환경 변수에 NOTION_API_KEY를 설정해야 합니다.")
    sys.exit(1)
if not GOOGLE_CLOUD_PROJECT or not GOOGLE_CLOUD_LOCATION:
    print("오류: .config 파일 또는 환경 변수에 GOOGLE_CLOUD_PROJECT와 GOOGLE_CLOUD_LOCATION을 설정해야 합니다.")
    sys.exit(1)

# Vertex AI 초기화
try:
    vertexai.init(project=GOOGLE_CLOUD_PROJECT, location=GOOGLE_CLOUD_LOCATION)
except Exception as e:
    print(f"Vertex AI 초기화 중 오류 발생: {e}")
    print("gcloud auth application-default login 명령을 실행하여 인증했는지 확인하세요.")
    sys.exit(1)

# --- 함수 정의 ---

def markdown_to_notion_blocks(markdown_text):
    """
    마크다운 텍스트를 Notion 블록 객체 리스트로 변환합니다.
    인라인 **bold** 구문을 지원합니다.
    """
    blocks = []

    def parse_inline_bold(text):
        """텍스트 한 줄을 파싱하여 **bold** 마크업을 Notion rich_text 객체 리스트로 변환합니다."""
        parts = text.split('**')
        rich_text_objects = []
        for i, part in enumerate(parts):
            if not part:
                continue
            # 짝수 인덱스(0, 2, ...)는 일반 텍스트, 홀수 인덱스(1, 3, ...)는 굵은 텍스트입니다.
            is_bold = i % 2 != 0
            rich_text_objects.append({
                "type": "text",
                "text": {"content": part},
                "annotations": {"bold": is_bold}
            })
        return rich_text_objects

    for line in markdown_text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue

        if line.startswith('### '):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": parse_inline_bold(line[4:])}})
        elif line.startswith('## '):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": parse_inline_bold(line[3:])}})
        elif line.startswith('# '):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": parse_inline_bold(line[2:])}})
        elif line.startswith( ('- ', '* ') ):
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": parse_inline_bold(line[2:])}})
        elif line.startswith('> '):
            blocks.append({"type": "quote", "quote": {"rich_text": parse_inline_bold(line[2:])}})
        elif line == '---':
            blocks.append({"type": "divider", "divider": {}})
        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": parse_inline_bold(line)}})
    return blocks

def get_database_id_from_url_or_page(notion_client_instance, url: str) -> str | None:
    """
    Notion URL에서 데이터베이스 ID를 추출합니다.
    URL이 페이지를 가리키는 경우, 해당 페이지 내의 첫 번째 자식 데이터베이스 ID를 반환합니다.
    """
    try:
        # 1. URL에서 ID 부분을 먼저 추출합니다.
        clean_url = url.split('?')[0]
        page_or_db_id = clean_url.rstrip('/').split('/')[-1]

        if len(page_or_db_id) != 32:
             # URL 마지막 부분이 32자가 아니면 Notion ID가 아님
            parts = clean_url.rstrip('/').split('-')
            page_or_db_id = parts[-1]

        if len(page_or_db_id) != 32:
            return None

        # 2. 추출된 ID를 페이지 ID로 간주하고 자식 블록을 확인합니다.
        print(f"'{page_or_db_id}' ID를 페이지로 간주하고, 내부의 데이터베이스를 찾습니다...")
        try:
            response = notion_client_instance.blocks.children.list(block_id=page_or_db_id)
            for block in response.get("results", []):
                if block['type'] == 'child_database':
                    print(f"페이지 내에서 데이터베이스를 찾았습니다. ID: {block['id']}")
                    return block['id']
        except notion_client.errors.APIResponseError as e:
            # 페이지가 아니라는 오류가 발생하면, ID를 데이터베이스 ID로 간주하고 계속 진행합니다.
            # "Could not find a page with ID" 와 같은 오류를 확인합니다.
            if "find a page" in str(e):
                 print("페이지가 아닌 것으로 보입니다. ID를 데이터베이스 ID로 직접 사용합니다.")
                 return page_or_db_id
            else:
                # 다른 API 오류는 다시 발생시킵니다.
                raise e

        # 3. 페이지 내에 자식 데이터베이스가 없는 경우, 원래 ID를 데이터베이스 ID로 가정하고 반환합니다.
        print("페이지 내에 데이터베이스를 찾지 못했습니다. ID를 데이터베이스 ID로 직접 사용합니다.")
        return page_or_db_id

    except (IndexError, AttributeError):
        return None

def get_text_from_block(block):
    """Notion 블록에서 일반 텍스트를 추출합니다."""
    text_content = ""
    block_type = block.get('type')
    if block_type in block and 'rich_text' in block[block_type]:
        for rich_text in block[block_type]['rich_text']:
            if 'plain_text' in rich_text:
                text_content += rich_text['plain_text']
    return text_content.strip()

def summarize_and_create_notion_page(database_id: str, period_days: int):
    """
    지정된 Notion 데이터베이스의 내용을 요약하고 새 페이지를 생성합니다.
    """
    try:
        # Notion 클라이언트 초기화
        notion = notion_client.Client(auth=NOTION_API_KEY)

        # 데이터베이스 정보를 조회하여 제목(title) 속성의 실제 이름을 찾습니다.
        print("데이터베이스 스키마를 조회하여 제목 속성을 확인합니다...")
        db_info = notion.databases.retrieve(database_id=database_id)
        title_property_name = None
        date_property_name = None
        for prop_name, prop_details in db_info['properties'].items():
            if prop_details['type'] == 'title':
                title_property_name = prop_name
            if prop_details['type'] == 'date':
                date_property_name = prop_name

        if not title_property_name:
            print(f"오류: 데이터베이스({database_id})에서 제목(title) 속성을 찾을 수 없습니다.")
            return

        print(f"데이터베이스의 제목 속성 이름을 '{title_property_name}'(으)로 확인했습니다.")
        if date_property_name:
            print(f"데이터베이스의 날짜 속성 이름을 '{date_property_name}'(으)로 확인했습니다.")
        else:
            print("날짜 속성을 찾지 못했습니다. 'last_edited_time'을 기준으로 필터링합니다.")


        # 1. 특정 기간 동안의 데이터베이스 페이지 조회 (필터링 로직 개선)
        start_date = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
        
        db_filter = {
            "timestamp": "last_edited_time",
            "last_edited_time": {
                "on_or_after": start_date
            }
        }

        response = notion.databases.query(database_id=database_id, filter=db_filter)
        pages = response.get("results", [])

        if not pages:
            print(f"{period_days}일 내에 변경된 페이지가 없습니다.")
            return

        print(f"총 {len(pages)}개의 페이지를 발견했습니다. 내용을 추출합니다...")

        # 2. 페이지 콘텐츠 추출
        full_content = ""
        for page in pages:
            if title_property_name in page['properties'] and page['properties'][title_property_name].get('title'):
                 page_title_list = page['properties'][title_property_name]['title']
                 if page_title_list:
                    page_title = page_title_list[0]['plain_text']
                    full_content += f"## {page_title}\n\n"

            block_response = notion.blocks.children.list(block_id=page['id'])
            for block in block_response.get("results", []):
                block_text = get_text_from_block(block)
                if block_text:
                    full_content += block_text + "\n"
            full_content += "\n---\n\n"

        print("콘텐츠 추출 완료. Vertex AI Gemini API로 요약을 시작합니다...")

        # 3. Vertex AI Gemini API로 내용 요약 (프롬프트 수정)
        model = GenerativeModel("gemini-2.5-pro")
        prompt = f"""
        당신은 Notion 페이지용 문서를 요약하는 AI 어시스턴트입니다. 다음은 지난 {period_days}일 동안 기록된 여러 문서의 내용입니다.

        전체 내용을 종합적으로 분석하여 핵심 주제, 주요 논의사항, 결정된 액션 아이템을 중심으로 명확하게 요약해주세요.
        결과는 반드시 마크다운 형식이어야 합니다. 다른 대화나 설명 없이, 요약된 마크다운 문서 내용만 생성해주세요.

        --- 문서 내용 ---
        {full_content}
        --- 요약 시작 ---
        """
        
        summary_response = model.generate_content(prompt)
        summary_text = summary_response.text

        print("요약 완료. Notion에 새 페이지를 생성합니다...")

        # 4. 요약된 내용으로 새 Notion 페이지 생성 (마크다운 파서 사용)
        today_str = datetime.now().strftime('%Y년 %m월 %d일')
        new_page_title = f"[{today_str}] 지난 {period_days}일 활동 요약"
        
        children_blocks = markdown_to_notion_blocks(summary_text)

        new_page_properties = {
            title_property_name: {
                "title": [{"text": {"content": new_page_title}}]
            }
        }
        # 날짜 속성이 있는 경우에만 값을 추가
        if date_property_name:
            today_iso = datetime.now().strftime('%Y-%m-%d')
            new_page_properties[date_property_name] = {
                "date": {
                    "start": today_iso
                }
            }

        created_page = notion.pages.create(
            parent={"database_id": database_id},
            properties=new_page_properties,
            children=children_blocks
        )

        print(f"✅ 요약 페이지가 성공적으로 생성되었습니다!")
        print(f"🔗 URL: {created_page['url']}")

    except notion_client.errors.APIResponseError as e:
        print(f"Notion API 오류가 발생했습니다: {e}")
    except Exception as e:
        print(f"알 수 없는 오류가 발생했습니다: {e}")


# --- 스크립트 실행 ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Notion 데이터베이스의 페이지를 요약하여 새 페이지로 생성합니다.")
    parser.add_argument(
        "--database-url",
        required=True,
        help="요약할 Notion 데이터베이스 또는 해당 데이터베이스를 포함한 페이지의 전체 URL"
    )
    parser.add_argument(
        "--days",
        type=int,
        required=True,
        help="오늘로부터 며칠 전까지의 데이터를 요약할지 기간(일)"
    )
    
    args = parser.parse_args()
    
    # Notion 클라이언트를 먼저 초기화합니다.
    notion = notion_client.Client(auth=NOTION_API_KEY)
    
    database_id = get_database_id_from_url_or_page(notion, args.database_url)
    
    if not database_id:
        print("오류: 유효하지 않은 Notion URL이거나 페이지 내에서 데이터베이스를 찾을 수 없습니다.")
        sys.exit(1)
        
    summarize_and_create_notion_page(
        database_id=database_id,
        period_days=args.days
    )
