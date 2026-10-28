# TeamSpace Backend

TeamSpace 인하우스 웹용 FastAPI + PostgreSQL 백엔드입니다. 이 API는 `TeamSpace-frontend-main/lib/demo.ts`와 `lib/inhouse.ts`의 프론트 타입과 요청사항에 맞춰 구성했습니다.

## 로컬 실행

1. PostgreSQL에서 `teamspace` DB와 접속 역할을 만들고 `.env.example`을 `.env`로 복사합니다.
2. `.env`의 `DATABASE_URL`과 `JWT_SECRET_KEY`를 로컬 값으로 채웁니다. 비밀값은 저장소에 커밋하지 마세요.
3. PowerShell에서 다음을 실행합니다.

```powershell
Set-Location C:\Users\user\Desktop\TeamSpace
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

개발용 Ruff 포맷터와 FastAPI 테스트 클라이언트는 다음처럼 설치합니다.

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff format app main.py
```

앱 시작 시 SQLAlchemy 모델을 기준으로 없는 테이블을 생성합니다. 기존 DB 구조 변경은 자동 마이그레이션하지 않으므로 배포 전에는 Alembic 마이그레이션을 도입해야 합니다. OpenAPI UI: <http://127.0.0.1:8000/docs>

## 사용자 계정

- `py scripts/create_user.py`로 초기 사용자 계정을 등록합니다. 비밀번호는 Argon2 해시로 저장됩니다.
- 역할은 `Admin`, `PM`, `FA`, `Leader`, `Member`를 사용합니다.
- 가입 요청은 `POST /auth/signup-requests`로 저장되고, 관리자가 `/admin/signup-requests`에서 조회한 뒤 승인할 수 있습니다.
- 로그인 응답은 JWT access/refresh 토큰을 반환합니다. `/auth/refresh`로 갱신하고, 보호 API에는 `Authorization: Bearer <accessToken>`을 보냅니다.

## API 범위

- 디자인 보강: /todos와 /me/todos의 할 일 CRUD, 결재 검토 할 일, 프로젝트 목록 검색·부서·상태·모집 필터, 월별 일정 범위·검색 필터, /dashboard/home의 실제 사용자 데이터
- 커뮤니티 보강: 게시글 임시 저장·수정·이미지 5장 첨부, 디자인 카테고리, 운영 제안 목록·상세·댓글·추천·관리자 상태 변경, 권한 안전 통합 검색
- 결재 보강: 비용·인력 지원·프로젝트 요청·운영 요청·기타 내부 문서 유형과 업로드 소유권 검증

- 인증·프로필·팀원: `/auth/login`, `/auth/signup-requests`, `/auth/password-reset-*`, `/me`, `/people`
- 알림·설정·푸시: `/notifications`, `/me/notification-settings`, `/push/subscriptions`
- 재무: `/finance/summary`, `/finance/receipts`, `/finance/contracts/{project}`, 은행 계좌·동기화·가져온 거래·반영 이력(`/finance/bank-accounts`, `/finance/bank-syncs`, `/finance/bank-imports`, `/finance/bank-import-commits`) 및 `/admin/finance/*`
- 결재: `/approvals`와 승인·반려·취소·댓글 처리
- 문서함: `/docs`, 상세·개정 이력·읽음 확인
- 내 공간: `/me/summary`, `/me/activity`, `/me/projects`, `/projects/{id}`, `/events`
- 커뮤니티: 게시글·익명 댓글·공감·신고, 문의, 공지
- 관리자: 공지·일정·문서 개정·문의 답변·신고 처리·재정 권한·프로젝트 관리
- 파일: `POST /uploads`, 최대 10MB

목록은 `{ "items": [], "nextCursor": ... }` 형식이며, API 데이터 필드는 프론트 코드의 camelCase 타입에 맞춥니다. 익명 글·댓글에서는 `author` 값을 응답에 포함하지 않습니다. 재정 API는 `Admin`, `FA`, `Leader` 역할에만 허용합니다.

## 외부 서비스 설정

익명 글·댓글·문의는 DB resources.owner_id에 작성자의 사용자 ID를 저장하지 않고 author를 "익명"으로 기록합니다. 본인 활동 관리에는 비밀키 기반 익명 소유 키를 사용하며 API 응답에서 이 키를 제외합니다. 기존 익명 레코드와 해당 글의 첨부 파일도 서버 시작 시 직접 사용자 연결을 제거합니다. ANONYMOUS_AUTHOR_SECRET을 별도로 설정하면 JWT_SECRET_KEY 변경 후에도 익명 활동 소유권이 유지됩니다. 생략하면 JWT_SECRET_KEY에서 익명 소유 키를 계산하므로 JWT 키를 회전하기 전에 별도 익명 키를 설정해야 합니다.
- SMTP 메일 발송 코드는 `app/services/email.py`에 있으며, 설정은 프로젝트 루트 `.env`에서 읽습니다. `.env.example`의 `SMTP_*` 항목을 메일 제공자의 SMTP 값으로 채우면 비밀번호 재설정 메일이 발송됩니다.
- 일반적으로 포트 587은 `SMTP_USE_TLS=true`, `SMTP_USE_SSL=false`; 포트 465는 `SMTP_USE_TLS=false`, `SMTP_USE_SSL=true`로 설정합니다. 두 옵션을 동시에 켜지 마세요. 인증이 필요한 제공자는 계정 주소를 `SMTP_USERNAME`, 앱 비밀번호를 `SMTP_PASSWORD`에 입력합니다.
- 브라우저 푸시는 VAPID 키와 클라이언트 구독이 필요합니다. 설정하지 않아도 일반 알림 레코드는 저장되지만 실제 푸시는 전송하지 않습니다.
- 은행 거래는 `BANK_*` 환경 변수로 서버에만 설정합니다. 은행·펌뱅킹·오픈뱅킹 업체가 정해지지 않아 현재는 거래 임포트/중복 방지/매칭 검토/확인 후 재정 반영 API와 공급자 어댑터 경계까지 준비되어 있고, 실제 `sync`는 공급자 선택 및 인증 규격에 맞는 어댑터 구현 전까지 503으로 실패 이력을 남깁니다. 운영진이 은행 또는 조회 서비스와 계약한 뒤 해당 어댑터를 연결해야 자동 조회가 시작됩니다.
- 은행에서 불러온 거래는 자동 매칭만으로 재정에 반영되지 않습니다. 관리자가 프로젝트 연결을 확인하고 `confirm=true`로 확정한 뒤 `POST /finance/bank-import-commits`를 호출해야 합니다. 출금은 수금 내역 대신 지출 레코드로만 기록하며, 고유 거래 지문과 DB 고유 제약으로 중복 반영을 막습니다.
- 반영 배치 이력은 `GET /finance/bank-import-commits`에서 담당자, 시각, 건수로 조회할 수 있습니다. 거래별 반영자와 반영 시각도 거래 목록에 포함됩니다.
- 업로드 파일은 현재 로컬 `uploads/` 디렉터리에 저장됩니다. 배포 환경에서는 S3 등 영속 스토리지로 교체해야 합니다.

업무 레코드는 현재 `resources` 테이블의 JSON payload로 저장됩니다. 은행 거래 임포트와 동기화 이력은 고유 거래 키와 잠금이 필요한 별도 `bank_imports`, `bank_sync_runs` 테이블에 저장됩니다. 앱 시작 시 `create_all`이 신규 테이블을 생성합니다. 프론트엔드의 데모 데이터는 아직 API 호출로 교체하지 않았습니다.

신규 DB에는 등록한 사용자 외의 팀원·프로젝트·공지·규정·재무 자료가 자동으로 채워지지 않습니다. 관리자 API로 운영 데이터를 등록해야 목록에 표시됩니다.
