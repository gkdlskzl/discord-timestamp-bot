"""시간/타임존 유틸.

- 저장은 UTC(timezone-aware)로 하고, 집계·표시할 때만 로컬(KST 등)로 변환한다.
- '하루'의 기준 시각을 config(day_boundary_hour)로 조절한다.
  예) day_boundary_hour=4 이면 새벽 2시 공부는 '전날' 스터디로 집계된다.
"""

from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    """현재 시각(UTC, tz-aware)."""
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> datetime:
    """저장된 ISO8601 문자열 → tz-aware datetime."""
    return datetime.fromisoformat(s)


def fmt_duration(seconds: float) -> str:
    """초 → [HH:MM:SS] 문자열."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def day_window(now: datetime, boundary_hour: int, tz: ZoneInfo):
    """`now`가 속한 '스터디 하루'의 [시작, 끝) 구간을 UTC로 반환.

    스터디 하루는 로컬 기준 boundary_hour 시각에 시작해서 24시간 이어진다.
    """
    local = now.astimezone(tz)
    # 기준 시각만큼 당겨서 어느 '스터디 날짜'에 속하는지 판정
    shifted = local - timedelta(hours=boundary_hour)
    d = shifted.date()
    start_local = datetime.combine(d, time(0), tzinfo=tz) + timedelta(hours=boundary_hour)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def range_window(now: datetime, boundary_hour: int, tz: ZoneInfo, days: int):
    """오늘 포함 최근 `days`일의 [시작, 끝) 구간을 UTC로 반환."""
    start_today, end_today = day_window(now, boundary_hour, tz)
    start = start_today - timedelta(days=days - 1)
    return start, end_today


def overlap_seconds(sessions, w_start: datetime, w_end: datetime, now: datetime) -> float:
    """세션 목록과 [w_start, w_end) 구간의 겹치는 시간(초) 합계.

    자정 경계는 여기서 자연스럽게 처리된다(구간과의 교집합만 더함).
    end_utc가 없는(진행 중) 세션은 종료 시각을 `now`로 본다.
    """
    total = 0.0
    for s in sessions:
        st = parse_iso(s["start_utc"])
        en = parse_iso(s["end_utc"]) if s["end_utc"] else now
        lo = max(st, w_start)
        hi = min(en, w_end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total
