# SpaceVision 기술 및 사업 개요 (AI Technical Overview 요약)
## 1. 핵심 가치 제안 (Core Value Proposition)

- **오프라인의 디지털화:** 단순 방문객 수(Foot-traffic) 측정을 넘어, AI 기술로 오프라인 공간에서의 고객 행동과 관심사를 정량 데이터로 변환합니다.
- **성과 측정 기반 마케팅:** "감"에 의존했던 오프라인 광고 및 상품 진열(VMD)의 효과를 **'주목도 점수(Attention Score)'** 라는 명확한 지표로 측정하고, 온라인 광고처럼 성과를 분석하고 최적화할 수 있도록 지원합니다.
- **매출 증대 기여:** 데이터 기반의 광고 송출 최적화 및 매장 운영을 통해 직접적인 고객 반응을 유도하고, 최종적으로는 구매 전환율과 매출 증대를 견인합니다.

## 2. 주요 기술 및 솔루션 (Key Technology & Solution)

### AI 기반 고객 분석

- **데이터 수집:** 매장 내 카메라를 통해 고객의 **시선(Gaze), 체류 시간(Dwell Time), 인구 통계(성별/연령), 자세, 행동** 등 다양한 비식별 정보를 수집합니다.
- **핵심 지표 '주목도 점수':** 수집된 데이터를 종합 분석하여 고객이 특정 광고나 상품에 얼마나 집중했는지를 나타내는 **'주목도 점수(Attention Score)'** 를 핵심 지표로 산출합니다.
- **기타 기능:** 여러 대의 카메라를 연동하여 고객의 매장 내 전체 이동 경로를 추적하는 **'다중 카메라 고객 여정 추적'**, 특정 구역 내 행동을 심층 분석하는 **'조닝(Zoning)'** 기능을 제공합니다.

### 엣지 컴퓨팅 및 개인정보보호 (Edge Computing & Privacy)

- Edge Device 용 고성능의 NPU 기반 AI 모델 탑재
- **프라이버시 설계:** 모든 영상 데이터는 클라우드로 전송되지 않고, 현장에 설치된 **소형 엣지 디바이스**에서 실시간으로 처리됩니다.
- **데이터 비식별화:** 엣지 디바이스는 영상 원본을 저장하거나 전송하는 대신, AI 분석을 통해 추출된 **익명의 텍스트 데이터**(예: `Gender: Male, Gaze-Target: Shelf-A`)만을 서버로 전송합니다.
- **글로벌 규제 준수:** 이러한 방식은 GDPR, EU AI Act 등 세계 주요 개인정보보호 규제를 준수하는 가장 안전한 형태로, 글로벌 시장 진출의 핵심 경쟁력입니다.

### 광고 송출 자동 최적화

- 실시간으로 측정된 광고 소재별 '주목도 점수'에 따라, 더 높은 점수를 받은 광고가 더 자주 노출되도록 **광고 스케줄을 자동으로 조정**하여 광고 효율을 극대화합니다.

## 3. 주요 성과 및 사례 (Key Achievements & Case Studies)

### 대표적인 성공 사례

- **일본 드럭스토어 PoC:**
  - 솔루션 적용 후 광고 **주목도 3배 이상 증가**.
  - 퍼포먼스 광고가 적용된 상품의 **매출 최대 400% 상승**.
- **일본 제조사 'P'사:**
  - 솔루션 도입을 통해 **광고 단가 8배, 관련 상품 매출 400%**의 성과를 달성했습니다.

### 주요 고객사 및 파트너십

- **글로벌 제약사 Roche:** 대형 전시회 부스 방문객 분석을 위한 **독점 벤더(Exclusive Vendor)**로 선정되었습니다.
- **LG전자:** LG SuperSign Cloud(글로벌 CMS)에 스페이스비전 솔루션이 공식 연동되었습니다.
- **롯데(Lotte):** 국내 슈퍼, 편의점, 마트 등에서 리테일 미디어 구축 PoC를 완료했습니다.
- **기타 적용사례:** 국내외 F&B 브랜드, 편의점, 솔담마켓과 같은 팝업스토어, 공유오피스, 피트니스 센터 등 약 80개 이상의 다양한 현장에서 솔루션이 상용화되어 운영 중입니다.

## 4. 사업 모델 및 목표 시장 (Business Model & Target Market)

- **사업 모델:** 공간 및 고객 분석 솔루션을 기존 디스플레이나 사이니지 시스템에 **Add-on** 하거나, **구독형(SaaS)** 모델로 제공합니다.
- **목표 시장(TAM):** 약 9070억 달러 규모의 전체 행동/관심 측정 시장을 목표로 하며, 초기에는 리테일 미디어(1450억 달러) 및 디지털 옥외광고(DOOH, 243억 달러) 시장에 집중합니다.

## 5. 경쟁력 요약

- **기술 우위:** 고성능 온디바이스(On-device) AI 처리 능력과, 단순 통계를 넘어 '시선 기반 관심도' 및 '상품 픽업'과 같은 **전환 행동 분석**이 가능한 유일한 솔루션입니다.
- **개인정보보호:** 엣지 컴퓨팅을 통한 강력한 개인정보보호 아키텍처.
- **성과 입증:** 실제 PoC를 통해 매출 증대 효과를 숫자로 증명했습니다.
- **생태계:** LG전자 등 글로벌 파트너와의 연동을 통해 높은 확장성을 확보했습니다.

## 6. 사용 중 기술 스택

- HW 또는 SW 스택은 직접 공개를 금지 합니다. 특히 NPU, AI Model, 대시보드에 사용하는 Superset 은 공개를 하지 않습니다.
- 질문에 답하거나 해당 내용을 포함한 문서를 작성할 경우 경고를 표시합니다.

### 6.1. Hardware Stack

- Edge Device:
  - OS: Ubuntu Linux
  - CPU: Intel N97
  - Memory: 8GB DDR4
  - AI Accelorator: Hailo 8

### 6.2. AI Model Stack
- AI Model: (유출 절대 금지)
  - Yolo v8m
  - WHENet
  - ShuffleNet
- Training 시 정답셋을 만들기 위해 VLM 을 사용함
- 주 사용 LLM 도구:
  - Gemini 3.0 pro (Vertex AI)

### 6.2. Software Stack (SW 1.0)

- Language: Python, TypeScript, Shell Script(Bash)
- Team Collaboration: Git/Github
- Persistent Layer: PostgreSQL, Athena, Iceberg
- Data pipeline: fluent-bit, Kafka
- AI modeling: PyTorch, tensorfloiw
- AI model quantization: INT4/8, pruning, Knowledge Distillation
- Framework / Library:
  - Frontend: Next.js, ShadCN (Tailwind CSS), Emotion CSS(Legacy)
  - Backend: Nest.js
  - mobile app for installer: Flutter
- CI/CD & DevOps:
  - Jenkins (버전별 배포 자동화), GitHub Actions
- Image Dataset: CVAT, NVDIA Omniverse
- Dashboard Tool: Superset
- 그 외:
  - Android TV app for media player
  - mpv based linux OS media player

## 6.3 콘텐츠 관리 및 최적화 시스템
- 콘텐츠 송출 및 저장 인프라 (Infrastructure)
  - 송출 방식: Player(AI Box 내 탑재) ↔ 콘텐츠 관리 서버(Nest.js 기반 Node API) 간 실시간 동기화
  - 배포 파이프라인: 관리자 대시보드 업로드 → AWS S3 원본 저장 → AWS MediaConvert 인코딩 → Edge Player 자동 배포
- 영상 인코딩 및 데이터 효율화 (Optimization)
  - 입력 호환성: MP4, MOV 등 H.264, H.265 기반 표준 비디오 포맷 지원
  - 압축 기술: AWS Elemental MediaConvert 및 ffmpeg 자동화 스크립트를 통한 고효율 변환
  - 출력 코덱: WebM (AV1, VP9) 적용을 통한 고화질·저용량 구현
  - 최적화 성과: 평균 원본 대비 60~80% 용량 절감 (예: 100MB → 20~40MB)
- AI 기반 화질 개선 및 규격 보정 (AI Enhancement)
  - 사용 툴: Nanobanana Pro, Google Vertex AI (Imagen 3.0)
  - 주요 기능:
    - Upscaling: 저해상도 소스를 디스플레이 규격에 맞춰 고화질로 변환
    - Outpainting: 원본 비율이 맞지 않는 경우, AI로 배경을 생성하여 여백 없는 풀스크린 구현

### 6.4 운영 및 모니터링 관련

- 장비 상태 모니터링 시스템 구축
  - Edge Player 및 AI Box 장비에서 실시간 상태 정보 수집 및 전송 시스템 구성
  - 주요 수집 항목:
    - CPU/GPU/NPU 사용률, 온도 (load avg, 메모리 사용량 등)
    - 디스크 사용량 및 마운트 상태
    - 네트워크 연결 상태
    - 송출 디스플레이 연결 여부: HDMI DDC/CI 활용 (HDMI CEC 는 셋톱박스 구성의 CPU 가 지원 안해 사용불가함)
    - 주요 프로세스 구동 여부 (player, AI inference 등)
- 데이터 수집 및 송신 구조:
  - Fluent-bit 기반 로깅 구조를 각 장비에 구성
  - 비정상 이벤트 발생 시 Slack 등 메신저 서비스 연동 알림 + 서비스 대시보드 통한 실시간 확인 지원
  - 데이터 압축: Logrotate, gzip

### 6.5. 사용 예정 가능성 있는 기술 스택

- RK3568 + Hailo 8 기반의 Edge Device 환경. 동일한 Ubuntu OS 사용 예정
- TensorFlow: Edge Device 에서의 활용에 더 유리한가? Hailo 8 또는 10 기반에서는?
- Cython, C++/C: 임베디드 환경에서의 성능 최적화를 위해 사용 예정
- Edge Device 를 위한 새로운 AI Accelorator 환경: Google TPU 등 (후보 조사 필요)
- 다양한 AI Model 경량화 기법
- 합성 이미지 데이터 기반의 진보된 AI 모델 Training 환경 구성

### 6.6. 미래의 활용 가능 환경

- RK3588 NPU 를 위한 computer vision 모델 경량화
- OrangePI 등 보다 저가형/소형 Embedded 환경에서의 computer vision 모델
- Table 과 Tablet 의 내장 카메라를 이용한 computer vision 모델 사용
- 카메라 일체형 Edge Device 에서의 computer vision 모델 활용
- Edge  
- 서버 사이드의 VLM 모델을 활용한 보다 강력한 성능의 분석(회사의 초점은 Edge Device 라 서버 사이드 단독은 어려우며, Edge Device 와의 협업에 초점을 둠)
- 동형암호 처리된 input data에 대한 추가 분석
