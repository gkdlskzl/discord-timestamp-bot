"""Notion 연동 (Phase 3).

- 매일 정산된 유저별 순공/휴식 시간을 Notion 데이터베이스에 한 행(page)씩 추가한다.
- 프로퍼티 '이름'은 config에서 조절 가능(한글 DB도 지원). 설정된 프로퍼티만 전송한다.
- 의존성은 discord.py가 이미 쓰는 aiohttp 뿐.
"""

import logging

import aiohttp

from timeutils import fmt_duration

log = logging.getLogger("study-bot.notion")

NOTION_API = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"

# config.notion.props 미설정 시 기본 프로퍼티 이름(=Notion DB 컬럼명)
DEFAULT_PROPS = {
    "title": "이름",        # DB의 제목(title) 프로퍼티 이름
    "date": "날짜",         # date
    "study_hours": "순공(시간)",  # number
    "rest_hours": "휴식(시간)",   # number
    "study_text": "순공",   # rich_text (HH:MM:SS)
    "rest_text": "휴식",    # rich_text (HH:MM:SS)
}


class NotionClient:
    def __init__(self, token: str, database_id: str, props: dict = None):
        self.token = token
        self.database_id = database_id
        self.props = {**DEFAULT_PROPS, **(props or {})}
        self._session = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            })
        return self._session

    def _build_props(self, username, date_str, study_sec, rest_sec) -> dict:
        p = self.props
        out = {}
        if p.get("title"):
            out[p["title"]] = {"title": [{"text": {"content": username}}]}
        if p.get("date"):
            out[p["date"]] = {"date": {"start": date_str}}
        if p.get("study_hours"):
            out[p["study_hours"]] = {"number": round(study_sec / 3600, 2)}
        if p.get("rest_hours"):
            out[p["rest_hours"]] = {"number": round(rest_sec / 3600, 2)}
        if p.get("study_text"):
            out[p["study_text"]] = {"rich_text": [{"text": {"content": fmt_duration(study_sec)}}]}
        if p.get("rest_text"):
            out[p["rest_text"]] = {"rich_text": [{"text": {"content": fmt_duration(rest_sec)}}]}
        return out

    async def add_daily_record(self, username, date_str, study_sec, rest_sec) -> bool:
        """유저 하루치 기록을 Notion DB에 한 행 추가. 성공 여부 반환."""
        body = {
            "parent": {"database_id": self.database_id},
            "properties": self._build_props(username, date_str, study_sec, rest_sec),
        }
        try:
            sess = await self._sess()
            async with sess.post(NOTION_API, json=body) as resp:
                if resp.status >= 300:
                    text = await resp.text()
                    log.error("Notion 기록 실패(%s): %s", resp.status, text[:400])
                    return False
                return True
        except aiohttp.ClientError as e:
            log.error("Notion 요청 오류: %s", e)
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
