# 사내 테니스코트 예약 (A/B) – Google Sheets + Lock/OCC

3개 고정 시간대(점심 A/B, 퇴근 후 **17:00~18:00**)의 A/B 코트 예약 앱입니다.
- **Streamlit Community Cloud 무료 배포**
- **Google Sheets**를 저장소로 사용
- 날짜별 **부분 업데이트** + **낙관적 동시성 제어(버전)** + **Best-effort 락(만료 포함)**

## 1) 준비물
- Google Cloud 서비스 계정(JSON 키)
- Google Sheets 스프레드시트 1개 (편집 권한을 서비스 계정 이메일에 부여)

## 2) 파일 구성
- `app.py` — Streamlit 앱(메인 UI)
- `store_with_lock.py` — Google Sheets 저장소 + Lock/OCC 로직
- `requirements.txt` — 의존성
- `secrets.example.toml` — Streamlit Secrets 템플릿

## 3) 로컬 실행
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## 4) Streamlit Cloud 배포
1. GitHub에 위 4개 파일 업로드
2. Streamlit Cloud에서 **New app** → 레포 선택 → `app.py` 지정 → Deploy
3. **App → Settings → Secrets**에 다음 템플릿을 채워 넣기

```toml
[gcp_service_account]
# 서비스 계정 키 JSON 그대로 붙여넣기
# 예: type, project_id, private_key, client_email, token_uri 등

# 시트 ID: URL의 /d/와 /edit 사이 문자열
gsheet_id = "YOUR_SHEET_ID"

# (선택) 워크시트 이름 커스터마이즈 시 사용
# worksheet = "reservations"
```

> **중요:** 해당 스프레드시트를 서비스 계정 `client_email`과 **공유(편집자)** 해야 합니다.

## 5) 정책/운영 팁
- 동시 편집이 많다면 저장 실패 시 메시지(`LOCK_FAIL`, `VERSION_CONFLICT`)를 안내하고 **재시도/새로고침** 유도
- 일/주 최대 예약 횟수, 점검/휴장 시간, 관리자 대시보드 등은 별도 워크시트로 쉽게 확장 가능

## 6) 기본 슬롯 변경
`app.py` 상단의 `BLOCKS`를 수정하세요.
```python
BLOCKS = [
  {"id":"LUNCHA","label":"점심시간 A","start":"11:30","end":"12:15"},
  {"id":"LUNCHB","label":"점심시간 B","start":"12:15","end":"13:00"},
  {"id":"AFTER", "label":"퇴근 후",     "start":"17:00","end":"18:00"},
]
```
