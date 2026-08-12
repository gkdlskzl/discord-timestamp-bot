# 디스코드 스터디 시간 측정 봇 프로젝트 계획서 (v2)

> Raspberry Pi 5 (8GB) 자체 호스팅 / discord.py 기반 경량 봇
> 음성 채널 체류 시간을 목적별(순공/휴식)로 분리 측정하고, 휘발되지 않게 로컬 DB에 영구 기록한다.

---

## 1. 현재 문제 상황
* **기록의 휘발성:** 디스코드 기본 기능은 음성 채널 퇴장 시 누적 체류 시간이 즉시 사라져 개인 스터디 기록용으로 부적합.
* **기성 봇의 한계:** Statbot, ProBot 등은 불필요한 기능(음악, 미니게임)이 많아 무겁고, 세밀한 커스텀(채널별 시간 분리)이나 데이터 외부 반출에 제한(유료화)이 있음.
* **리소스 제약:** 24시간 구동 환경(라즈베리파이)에서 리소스 점유율을 최소화하는 가벼운 스크립트 필요.

## 2. 해결 방안
* **경량 커스텀 봇:** `discord.py`의 `on_voice_state_update` 이벤트만 감지하는 최소 코드로 구현.
* **채널별 목적 분리 측정:**
  - '공부를 해야만 하는 방' 등 → 순공 시간
  - '휴식을 취하는 방' 등 → 휴식 시간
  - 채널 ID → 종류(study/rest) 매핑은 config로 관리
* **자체 호스팅:** 라즈베리파이에 24시간 상시 기록. (Docker 컨테이너 또는 systemd 서비스)

---

## 3. 핵심 설계 결정 (v1 대비 개선 사항)

### 3-1. 이벤트 분기 처리 ⚠️ 가장 중요
`on_voice_state_update`는 입/퇴장뿐 아니라 **마이크 뮤트, 헤드셋 뮤트, 화면공유 시작/종료** 때도 발생한다. `before.channel`과 `after.channel`을 비교해 아래처럼 분기해야 시간이 틀어지지 않는다.

| 조건 | 의미 | 처리 |
|---|---|---|
| `before.channel is None` | 입장 | 세션 시작 (타임스탬프 기록) |
| `after.channel is None` | 퇴장 | 세션 종료 → DB 기록 |
| 둘 다 있고 서로 다름 | 채널 이동 | 기존 세션 종료 + 새 세션 시작 |
| 둘 다 있고 같음 | 뮤트/화면공유 등 | **무시** |

### 3-2. 저장소: SQLite (세션 단위 원자 기록)
* `.txt`/`.json` append 방식은 쓰기 중 정전 시 파일 손상 위험 (SD카드 특히 취약). SQLite는 표준 라이브러리(설치 불필요) + 트랜잭션으로 안전.
* **누적 합계를 저장하지 않고 세션 단위로 기록**한다. 합계는 `SUM`으로 계산 → 통계 재산출 자유, 값 드리프트 없음.
* WAL 모드 사용 (작은 쓰기, SD카드 마모 최소화).

**세션 테이블 스키마 (안)**
```sql
CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    channel_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,          -- 'study' | 'rest'
    start_utc  TEXT NOT NULL,          -- ISO8601, timezone-aware (UTC)
    end_utc    TEXT,                   -- NULL이면 진행 중 세션
    duration_sec INTEGER               -- 종료 시 계산
);
```

### 3-3. 자정 경계 처리
* **원본 세션은 그대로 저장하고, 집계(정산)할 때 자정 기준으로 쪼갠다.**
  - 예: 23:40~01:10(90분) → 당일 20분 + 익일 70분
* 원본 무손실이라 정책 변경 시 재계산 가능.
* **하루 기준 시각을 config로 조절 가능하게 한다** (`DAY_BOUNDARY_HOUR`). 기본 `0`(자정). 새벽까지 공부하는 팀이면 `4`(새벽 4시)로 설정 시 "새벽 공부 = 전날 공부"로 집계됨.

### 3-4. 재시작 대응 (휘발성의 진짜 해결)
* 입장 타임스탬프를 메모리에만 두면 봇 재시작 시 진행 중 세션 유실.
* **봇 시작 시** `guild.voice_channels`를 스캔해 이미 접속 중인 멤버들의 세션을 현재 시각 기준으로 다시 시작(open).
* 진행 중 세션도 DB에 `end_utc=NULL`로 기록해두고, 재시작 시 이를 복구/정리.
* 배포는 **자동 재시작 설정** 필수 (Docker `restart: unless-stopped` 또는 systemd `Restart=always`).

### 3-5. 타임존
* 저장은 **UTC(timezone-aware, `datetime.now(timezone.utc)`)**, 표시·집계 시 KST 변환.
* 라즈베리파이 시스템 시간도 KST로 맞추고 NTP 동기화 확인.

### 3-6. 설정/보안 분리
* 봇 토큰은 `.env`(python-dotenv)로 분리, `.gitignore`에 등록. 코드 하드코딩 금지.
* 채널 ID→종류 매핑, 로그 출력 채널 ID, `DAY_BOUNDARY_HOUR` 등은 `config.json`(또는 `.env`)로 관리.

### 3-7. 디스코드 개발자 포털 설정 (자주 막히는 지점)
* Bot 생성 후 토큰 발급.
* **Privileged Gateway Intents에서 필요한 Intent 활성화**, 코드에서 `intents.voice_states = True` 설정. 안 하면 이벤트가 아예 안 들어옴.

---

## 4. 단계별 개발 계획

### Phase 1: 핵심 시간 측정 로직
* 개발자 포털 봇 생성 / 토큰 발급 / Intent 설정.
* `on_voice_state_update` 이벤트 **4분기 처리**(입장/퇴장/이동/무시).
* 입장 시 타임스탬프 기록, 퇴장 시 `[HH:MM:SS]`로 변환.
* 지정 텍스트 채널에 로그 메시지 출력.
* **재시작 시 접속자 스캔**으로 진행 중 세션 복구.

### Phase 2: 채널 분류 및 로컬 DB 저장
* 채널 ID → 순공/휴식 분류.
* SQLite에 세션 단위 기록 (WAL 모드).
* 진행 중 세션(`end_utc=NULL`) 관리 및 종료 시 `duration_sec` 계산.
* 기본 조회: 유저별/일별 순공·휴식 시간.

### Phase 3: 외부 지식 관리 툴 연동 (Notion) — 확장, 나중에
* 매일 설정 시각(`DAY_BOUNDARY_HOUR`) 기준 일일 정산 (자정 경계 분할 적용).
* **Notion API로 당일 순공 시간/로그 자동 전송·아카이빙.**
* 연동 시점에 필요한 준비 (지금은 불필요):
  1. Notion Integration 생성 및 토큰 발급
  2. 저장할 데이터베이스(페이지)를 해당 Integration에 Connections 연결
* discord.py `tasks.loop`로 스케줄링(별도 cron 불필요, 단일 프로세스 유지).

---

## 5. 리소스 견적 (Raspberry Pi 5 / 8GB / Docker 병행)

| 항목 | 예상치 | 비고 |
|---|---|---|
| RAM | ~100–150 MB | 전체 8GB 중 약 1.5~2%. Python + discord.py 상주분 |
| CPU | 24h 평균 1% 미만 | 이벤트 시 순간 스파이크, 웹소켓 heartbeat는 무시 가능 |
| 디스크 | 하루 수십~수백 KB | SQLite. 1년 누적 수십 MB |
| 네트워크 | 분당 수 KB | 게이트웨이 웹소켓 유지 |

* 기존 Docker 스택과 자원 경쟁 사실상 없음.
* **배포 방식 선택지**
  - **Docker 컨테이너** (`python:3.12-slim`, +~20–40MB): 기존 스택과 관리 통일, `restart: unless-stopped`로 자동 재시작. → 이미 Docker 사용 중이므로 **권장**.
  - **systemd 베어 실행** (~100MB, 오버헤드 0): 가장 가벼움, `Restart=always`.

---

## 6. 산출물 (구현 시)
* `bot.py` — 메인 봇 (이벤트 처리 + 세션 관리)
* `db.py` — SQLite 래퍼 (세션 기록/조회)
* `config.example.json` — 채널 매핑/설정 템플릿
* `.env.example` — 토큰 템플릿
* `requirements.txt`
* `Dockerfile` + `docker-compose.yml`
* `study-timer-bot.service` — systemd 유닛 (대안)
* `README.md` — 셋업 가이드 (개발자 포털 설정 포함)

---

## 7. v1 → v2 변경 요약
| 항목 | v1 | v2 |
|---|---|---|
| 저장소 | txt/json | **SQLite (세션 단위)** |
| 이벤트 처리 | 입/퇴장 | **입/퇴장/이동/무시 4분기** |
| 재시작 | 언급 없음 | **접속자 스캔 복구 + 자동 재시작** |
| 자정 경계 | 미정 | **원본 저장 + 집계 시 분할, 기준 시각 config화** |
| 배포 | "백그라운드" | **Docker(권장) / systemd** |
| 설정/보안 | - | **.env 토큰 분리, config 매핑** |
| 타임존 | 미정 | **UTC 저장 / KST 표시** |
