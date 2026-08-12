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

## 로드맵

- Phase 3: 매일 자정 정산 → **Notion** 대시보드 자동 전송 (예정)
