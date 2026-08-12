"""디스코드 스터디 시간 측정 봇.

- on_voice_state_update 를 4분기(입장/퇴장/이동/무시)로 처리한다.
- 채널을 순공(study)/휴식(rest)으로 분류해 세션 단위로 SQLite에 기록한다.
- 봇 재시작 시 현재 접속자를 스캔해 진행 중 세션을 복구한다.
- 슬래시 커맨드로 본인/서버 통계를 조회한다.
"""

import asyncio
import json
import logging
import os
from datetime import timedelta

import discord
from discord import app_commands
from dotenv import load_dotenv

from db import Database
from notion import NotionClient
from timeutils import (
    day_window,
    fmt_duration,
    now_utc,
    overlap_seconds,
    parse_iso,
    range_window,
)
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("study-bot")

KIND_LABEL = {"study": "📚 순공", "rest": "☕ 휴식"}


def load_config(path: str = None) -> dict:
    path = path or os.getenv("CONFIG_PATH", "config.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class StudyBot(discord.Client):
    """discord.Client 기반 (슬래시 커맨드는 app_commands.CommandTree 사용)."""

    def __init__(self, config: dict):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True   # 음성 이벤트 (privileged 아님)
        intents.members = True        # 재시작 시 접속자 스캔 (portal에서 활성화 필요)
        super().__init__(intents=intents)

        self.config = config
        self.tz = ZoneInfo(config.get("timezone", "Asia/Seoul"))
        self.boundary = int(config.get("day_boundary_hour", 0))
        self.channels = {int(cid): kind for cid, kind in config.get("channels", {}).items()}
        self.log_channel_id = config.get("log_channel_id")
        self.guild_id = config.get("guild_id")
        self.post_on_join = config.get("post_on_join", False)
        self.post_on_leave = config.get("post_on_leave", True)
        self.db = Database(config.get("db_path", "data/study.db"))
        self.tree = app_commands.CommandTree(self)
        self._reconciled = False

        # Notion 연동 (선택). enabled + 토큰 + database_id 모두 있어야 활성화.
        ncfg = config.get("notion") or {}
        token = os.getenv("NOTION_TOKEN")
        if ncfg.get("enabled") and token and ncfg.get("database_id"):
            self.notion = NotionClient(token, ncfg["database_id"], ncfg.get("props"))
            log.info("Notion 연동 활성화됨.")
        else:
            self.notion = None
            if ncfg.get("enabled"):
                log.warning("Notion 연동이 켜져 있지만 NOTION_TOKEN 또는 database_id 누락 → 비활성화.")

        register_commands(self)

    # --- 라이프사이클 ----------------------------------------------------

    async def setup_hook(self):
        # 지정 길드가 있으면 그 길드에만 sync(즉시 반영). 없으면 글로벌(최대 1시간).
        if self.guild_id:
            guild = discord.Object(id=int(self.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("슬래시 커맨드를 길드 %s 에 동기화했습니다.", self.guild_id)
        else:
            await self.tree.sync()
            log.info("슬래시 커맨드를 글로벌로 동기화했습니다(반영에 시간이 걸릴 수 있음).")

        if self.notion:
            self.loop.create_task(self.settlement_loop())

    async def on_ready(self):
        log.info("로그인: %s (id=%s)", self.user, self.user.id)
        if not self._reconciled:
            self._reconciled = True
            await self.reconcile()

    def classify(self, channel: discord.VoiceChannel):
        """추적 대상 채널이면 kind, 아니면 None."""
        if channel is None:
            return None
        return self.channels.get(channel.id)

    # --- 재시작 복구 -----------------------------------------------------

    async def reconcile(self):
        """봇 다운타임 동안의 상태를 현재 접속 현황과 맞춘다.

        - 지금 추적 채널에 접속 중 + DB에 같은 채널 열린 세션 → 유지(시작시각 보존)
        - 접속 중인데 열린 세션이 없거나 다른 채널 → 지금 시각으로 새 세션 시작
        - DB엔 열려 있는데 지금 그 채널에 없음 → 다운타임 중 이탈로 보고 start 시각에 종료(0초 처리)
        """
        now = now_utc().isoformat()
        connected = {}  # user_id -> (channel, kind, member)
        for guild in self.guilds:
            for vc in guild.voice_channels:
                kind = self.classify(vc)
                if not kind:
                    continue
                for m in vc.members:
                    if m.bot:
                        continue
                    connected[m.id] = (vc, kind, m)

        open_by_user = {r["user_id"]: r for r in self.db.get_all_open_sessions()}

        # 접속 중인 유저 처리
        for uid, (vc, kind, m) in connected.items():
            r = open_by_user.get(uid)
            if r and r["channel_id"] == vc.id:
                continue  # 같은 채널 → 세션 유지
            self.db.open_session(uid, str(m), vc.id, kind, now)
            log.info("복구: %s 세션 시작(%s)", m, vc.name)

        # DB엔 열려있지만 지금 해당 채널에 없는 세션 종료
        for uid, r in open_by_user.items():
            conn = connected.get(uid)
            if conn and conn[0].id == r["channel_id"]:
                continue
            self.db.close_session(r["id"], r["start_utc"])  # 종료 시각 불명 → 0초 처리
            log.warning("복구: 유실 세션 종료(user=%s, ch=%s)", uid, r["channel_id"])

    # --- 음성 이벤트 -----------------------------------------------------

    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        # 뮤트/헤드셋/화면공유 등 같은 채널 내 상태 변화는 무시
        if before.channel == after.channel:
            return

        now = now_utc().isoformat()

        # 퇴장 or 이동: 이전 채널이 추적 대상이면 세션 종료
        if self.classify(before.channel):
            closed = self.db.close_open_for_user(member.id, now)
            if closed and self.post_on_leave:
                await self.post_leave(member, before.channel, closed)

        # 입장 or 이동: 이후 채널이 추적 대상이면 세션 시작
        kind = self.classify(after.channel)
        if kind:
            self.db.open_session(member.id, str(member), after.channel.id, kind, now)
            if self.post_on_join:
                await self.post_join(member, after.channel, kind)

    # --- 로그 채널 출력 --------------------------------------------------

    def _log_channel(self):
        if not self.log_channel_id:
            return None
        return self.get_channel(int(self.log_channel_id))

    async def post_leave(self, member, channel, closed_row):
        ch = self._log_channel()
        if ch is None:
            return
        kind = closed_row["kind"]
        now = now_utc()
        w_start, w_end = day_window(now, self.boundary, self.tz)
        study_today = self._sum(member.id, "study", w_start, w_end, now)
        embed = discord.Embed(
            title=f"{KIND_LABEL.get(kind, kind)} 종료",
            description=(
                f"{member.mention} · `{channel.name}`\n"
                f"체류: **{fmt_duration(closed_row['duration_sec'] or 0)}**\n"
                f"오늘 누적 순공: **{fmt_duration(study_today)}**"
            ),
            color=0x5865F2,
        )
        try:
            await ch.send(embed=embed)
        except discord.DiscordException as e:
            log.warning("로그 전송 실패: %s", e)

    async def post_join(self, member, channel, kind):
        ch = self._log_channel()
        if ch is None:
            return
        embed = discord.Embed(
            title=f"{KIND_LABEL.get(kind, kind)} 시작",
            description=f"{member.mention} · `{channel.name}`",
            color=0x57F287,
        )
        try:
            await ch.send(embed=embed)
        except discord.DiscordException as e:
            log.warning("로그 전송 실패: %s", e)

    # --- 집계 헬퍼 -------------------------------------------------------

    def _sum(self, user_id, kind, w_start, w_end, now):
        rows = self.db.sessions_in_window(
            w_start.isoformat(), w_end.isoformat(), user_id=user_id, kind=kind
        )
        return overlap_seconds(rows, w_start, w_end, now)

    # --- Notion 일일 정산 -----------------------------------------------

    async def settlement_loop(self):
        """매일 day_boundary_hour 시각에, 방금 끝난 하루를 정산해 Notion으로 전송."""
        await self.wait_until_ready()
        while not self.is_closed():
            now = now_utc()
            _, w_end = day_window(now, self.boundary, self.tz)  # 다음 경계(=오늘 창의 끝)
            wait = (w_end - now).total_seconds()
            if wait > 0:
                await asyncio.sleep(wait)
            settle_end = w_end
            settle_start = settle_end - timedelta(days=1)
            try:
                ok, total = await self.run_settlement(settle_start, settle_end)
                await self._post_settle_summary(settle_start, ok, total)
            except Exception:
                log.exception("정산 중 오류")
            await asyncio.sleep(2)  # 경계 재계산 안정화

    async def run_settlement(self, w_start, w_end):
        """[w_start, w_end) 하루를 유저별로 집계해 Notion에 기록. (성공수, 대상수) 반환."""
        if not self.notion:
            return 0, 0
        rows = self.db.sessions_in_window(w_start.isoformat(), w_end.isoformat())
        users = {}
        for r in rows:
            u = users.setdefault(r["user_id"], {"name": None, "rows": []})
            u["rows"].append(r)
            if r["username"]:
                u["name"] = r["username"]
        date_str = w_start.astimezone(self.tz).date().isoformat()
        ok = 0
        total = 0
        for uid, u in users.items():
            study = overlap_seconds(
                [r for r in u["rows"] if r["kind"] == "study"], w_start, w_end, w_end)
            rest = overlap_seconds(
                [r for r in u["rows"] if r["kind"] == "rest"], w_start, w_end, w_end)
            if study == 0 and rest == 0:
                continue
            total += 1
            if await self.notion.add_daily_record(u["name"] or str(uid), date_str, study, rest):
                ok += 1
        log.info("정산 %s: %d/%d명 Notion 기록", date_str, ok, total)
        return ok, total

    async def _post_settle_summary(self, w_start, ok, total):
        ch = self._log_channel()
        if ch is None or total == 0:
            return
        date_str = w_start.astimezone(self.tz).date().isoformat()
        try:
            await ch.send(embed=discord.Embed(
                title="🗓️ 일일 정산 완료",
                description=f"`{date_str}` · {ok}/{total}명 Notion 기록",
                color=0xEB459E,
            ))
        except discord.DiscordException:
            pass

    async def close(self):
        if self.notion:
            await self.notion.close()
        await super().close()


# --- 슬래시 커맨드 -------------------------------------------------------

def register_commands(bot: "StudyBot"):

    @bot.tree.command(name="today", description="오늘 내 순공/휴식 시간")
    async def today(interaction: discord.Interaction):
        now = now_utc()
        w_start, w_end = day_window(now, bot.boundary, bot.tz)
        study = bot._sum(interaction.user.id, "study", w_start, w_end, now)
        rest = bot._sum(interaction.user.id, "rest", w_start, w_end, now)
        embed = discord.Embed(title="📅 오늘 기록", color=0x5865F2)
        embed.add_field(name="📚 순공", value=f"**{fmt_duration(study)}**", inline=True)
        embed.add_field(name="☕ 휴식", value=f"**{fmt_duration(rest)}**", inline=True)
        embed.set_footer(text=f"{interaction.user.display_name} · 기준 {bot.boundary}시")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="week", description="최근 7일 순공 시간")
    async def week(interaction: discord.Interaction):
        now = now_utc()
        w_start, w_end = range_window(now, bot.boundary, bot.tz, 7)
        study = bot._sum(interaction.user.id, "study", w_start, w_end, now)
        rest = bot._sum(interaction.user.id, "rest", w_start, w_end, now)
        embed = discord.Embed(title="🗓️ 최근 7일", color=0x5865F2)
        embed.add_field(name="📚 순공", value=f"**{fmt_duration(study)}**", inline=True)
        embed.add_field(name="☕ 휴식", value=f"**{fmt_duration(rest)}**", inline=True)
        embed.set_footer(text=f"일 평균 순공 {fmt_duration(study / 7)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="now", description="지금 진행 중인 내 세션")
    async def now_cmd(interaction: discord.Interaction):
        row = bot.db.get_open_session(interaction.user.id)
        if row is None:
            await interaction.response.send_message(
                "진행 중인 세션이 없어요.", ephemeral=True
            )
            return
        elapsed = (now_utc() - parse_iso(row["start_utc"])).total_seconds()
        kind = KIND_LABEL.get(row["kind"], row["kind"])
        await interaction.response.send_message(
            f"{kind} 진행 중 · 경과 **{fmt_duration(elapsed)}**", ephemeral=True
        )

    @bot.tree.command(name="rank", description="오늘 순공 랭킹")
    async def rank(interaction: discord.Interaction):
        now = now_utc()
        w_start, w_end = day_window(now, bot.boundary, bot.tz)
        rows = bot.db.sessions_in_window(
            w_start.isoformat(), w_end.isoformat(), kind="study"
        )
        # 유저별 합산
        by_user = {}
        names = {}
        for r in rows:
            secs = overlap_seconds([r], w_start, w_end, now)
            by_user[r["user_id"]] = by_user.get(r["user_id"], 0) + secs
            names[r["user_id"]] = r["username"] or str(r["user_id"])
        ranking = sorted(by_user.items(), key=lambda x: x[1], reverse=True)[:10]
        if not ranking:
            await interaction.response.send_message(
                "오늘 기록이 아직 없어요.", ephemeral=True
            )
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, secs) in enumerate(ranking):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{prefix} {names[uid]} — **{fmt_duration(secs)}**")
        embed = discord.Embed(
            title="🏆 오늘 순공 랭킹", description="\n".join(lines), color=0xFEE75C
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="notion_sync",
                      description="[관리자] 지정한 날의 기록을 Notion으로 수동 전송")
    @app_commands.describe(days_ago="며칠 전 (1=어제, 0=오늘 현재까지)")
    async def notion_sync(interaction: discord.Interaction, days_ago: int = 1):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("관리자만 사용할 수 있어요.", ephemeral=True)
            return
        if not bot.notion:
            await interaction.response.send_message(
                "Notion 연동이 비활성화 상태예요 (config/NOTION_TOKEN 확인).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        now = now_utc()
        start_today, _ = day_window(now, bot.boundary, bot.tz)
        w_start = start_today - timedelta(days=days_ago)
        w_end = min(w_start + timedelta(days=1), now)  # 오늘(0)이면 현재까지만
        ok, total = await bot.run_settlement(w_start, w_end)
        date_str = w_start.astimezone(bot.tz).date().isoformat()
        await interaction.followup.send(
            f"`{date_str}` 정산 → {ok}/{total}명 Notion 전송 완료.", ephemeral=True)


def main():
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("환경변수 DISCORD_TOKEN 이 필요합니다 (.env 참고).")
    config = load_config()
    bot = StudyBot(config)
    bot.run(token)


if __name__ == "__main__":
    main()
