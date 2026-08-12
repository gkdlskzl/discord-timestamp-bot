# Discord Study Timer Bot

음성 채널 체류 시간을 **순공(study) / 휴식(rest)** 으로 분리 측정해서, 휘발되지 않게
로컬 SQLite에 영구 기록하는 경량 디스코드 봇. 라즈베리파이 24시간 자체 호스팅용.

자세한 설계 배경은 [`PLAN.md`](./PLAN.md) 참고.

## 기능

- `on_voice_state_update` **4분기 처리** — 입장 / 퇴장 / 채널 이동 / (뮤트·화면공유는 무시)
- 채널을 순공·휴식으로 분류해 **세션 단위**로 SQLite(WAL)에 기록
- **재시작 복구** — 봇이 켜질 때 현재 접속자를 스캔해 진행 중 세션을 이어감
- **자정 경계 처리** — 원본은 그대로 저장하고 집계 시 구간 교집합으로 계산 (`day_boundary_hour`로 하루 기준 시각 조절)
- 슬래시 커맨드: `/today` `/week` `/now` `/rank`

## 슬래시 커맨드

| 명령 | 설명 |
|---|---|
| `/today` | 오늘 내 순공/휴식 시간 (본인만 보임) |
| `/week` | 최근 7일 순공/휴식 + 일 평균 |
| `/now` | 지금 진행 중인 내 세션 경과 시간 |
| `/rank` | 오늘 서버 순공 랭킹 Top 10 |
| `/notion_sync` | [관리자] 지정한 날 기록을 Notion으로 수동 전송 (`days_ago` 1=어제, 0=오늘) |

## 1. 디스코드 개발자 포털 설정

1. https://discord.com/developers/applications → **New Application**
2. **Bot** 탭 → **Reset Token** 으로 토큰 발급 (이 값을 `.env`에)
3. **Privileged Gateway Intents** 에서 **SERVER MEMBERS INTENT** 켜기
   (재시작 복구 시 접속자 스캔에 필요)
4. **OAuth2 → URL Generator** → scopes `bot`, `applications.commands` 체크 →
   권한은 최소로 (메시지 전송 정도) → 생성된 URL로 서버에 초대

> 채널 ID / 서버 ID는 디스코드 앱에서 **개발자 모드**(설정 → 고급)를 켠 뒤
> 대상을 우클릭 → "ID 복사".

## 2. 설정 파일

```bash
cp config.example.json config.json
cp .env.example .env
```

- `.env` → `DISCORD_TOKEN` 채우기
- `config.json` 편집:
  - `guild_id` — 서버 ID (슬래시 커맨드 즉시 반영용)
  - `log_channel_id` — 로그를 출력할 텍스트 채널 ID (원치 않으면 그대로 둬도 됨)
  - `channels` — `"채널ID": "study"` 또는 `"rest"` 매핑
  - `day_boundary_hour` — 하루 기준 시각(0=자정). 새벽까지 공부하면 `4` 추천
  - `timezone` — 기본 `Asia/Seoul`

## 3. 실행

### A. Docker (권장 — 기존 스택과 관리 통일)

```bash
docker compose up -d --build
docker compose logs -f       # 로그 확인
```
`restart: unless-stopped` 라서 크래시/재부팅 시 자동 복구됩니다.

### B. systemd (베어 실행, 가장 가벼움)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo cp study-timer-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/study-timer-bot.service   # 경로/User 수정
sudo systemctl daemon-reload
sudo systemctl enable --now study-timer-bot
journalctl -u study-timer-bot -f
```

### C. 그냥 실행 (테스트용)

```bash
pip install -r requirements.txt
python bot.py
```

## 리소스 (라즈베리파이 5 / 8GB 기준)

RAM ~100–150MB, CPU 24h 평균 1% 미만, 디스크 하루 수십~수백 KB.
기존 Docker 컨테이너들과 자원 경쟁은 사실상 없습니다.

## 데이터

`data/study.db` (SQLite). 세션 단위로 쌓이며, 통계는 항상 여기서 재계산됩니다.

```sql
-- 예: 특정 유저 전체 순공 시간
SELECT SUM(duration_sec)/3600.0 AS hours
FROM sessions WHERE user_id = ? AND kind = 'study' AND end_utc IS NOT NULL;
```

## Phase 3: Notion 연동 (선택)

매일 `day_boundary_hour` 시각에 방금 끝난 하루를 유저별로 정산해서 Notion DB에
한 행씩 자동 추가한다. 자정 경계도 반영되어 그날 몫만 집계된다.

### 1) Notion 준비
1. https://www.notion.so/my-integrations → **New integration** →
   이름 지정 → **Internal Integration Secret** 복사 (`.env`의 `NOTION_TOKEN`)
2. Notion에서 **데이터베이스(표)** 하나 생성. 아래 프로퍼티(컬럼)를 **이름·타입 정확히** 맞춰 생성:

   | 프로퍼티 이름 | 타입 |
   |---|---|
   | `이름` | 제목(Title) |
   | `날짜` | 날짜(Date) |
   | `순공(시간)` | 숫자(Number) |
   | `휴식(시간)` | 숫자(Number) |
   | `순공` | 텍스트(Text) |
   | `휴식` | 텍스트(Text) |

   > 컬럼명을 바꾸고 싶으면 `config.json`의 `notion.props`에서 매핑하면 된다.
   > 불필요한 컬럼은 `props`에서 빼면 그 항목은 전송하지 않는다.
3. 그 데이터베이스 우상단 **••• → Connections(연결) → 만든 Integration 추가**
   (이걸 안 하면 봇이 DB에 못 씀 → 권한 오류)
4. 데이터베이스 **ID** 복사: DB를 풀페이지로 열고 URL의
   `notion.so/<워크스페이스>/<32자리 ID>?v=...` 에서 `<32자리 ID>` 부분.

### 2) 설정
- `.env` 에 `NOTION_TOKEN=secret_...`
- `config.json` 의 `notion` 블록:
  ```json
  "notion": {
    "enabled": true,
    "database_id": "여기에_32자리_DB_ID",
    "props": { "title": "이름", "date": "날짜",
               "study_hours": "순공(시간)", "rest_hours": "휴식(시간)",
               "study_text": "순공", "rest_text": "휴식" }
  }
  ```

### 3) 적용 & 확인
```bash
docker compose restart
```
- 관리자 계정에서 `/notion_sync days_ago:0` 실행 → Notion DB에 오늘치 행이 생기면 성공.
- 이후 매일 `day_boundary_hour` 시각에 전날치가 자동으로 쌓인다.

> Notion을 끄고 싶으면 `notion.enabled`를 `false`로. 봇의 나머지 기능은 그대로 동작한다.
> 봇이 꺼져 있어 놓친 날은 `/notion_sync days_ago:N`으로 수동 백필 가능.
