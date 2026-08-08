"""Discord 봇 — 조회·가벼운 제어 + 장중 대시보드.

역할 분담:
- **봇**: 상태 조회, 예수금, 요약·차트 요청, 감시 시작/중지, 알림 수준 변경.
  주문을 내는 조작(수동 청산·종목 삭제·설정 변경)은 **일부러 넣지 않는다** —
  계정이 탈취돼도 최악이 '감시가 꺼지는 것'에 그치도록 하기 위해서다.
- **웹훅(notifier)**: 실시간 알림 발송. 봇 연결이 끊겨도 알림은 계속 나간다.

대시보드는 감시 시작 시 만들어 채널에 고정하고, 주기적으로 **편집**해 갱신한다
(새 메시지를 만들지 않으므로 채널이 더러워지지 않고 푸시도 울리지 않는다).
일일 요약이 나가면 고정을 풀고 삭제해, 장 마감 후 채널에는 요약만 남는다.

봇은 코어와 같은 asyncio 루프에서 태스크로 돌아간다. 코어 상태를 직접 읽되
**변경은 반드시 명령 큐를 통해서** 한다 — UI 와 똑같은 경로라 경쟁 상태가 없다.
"""

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:  # 코어는 타입 표기용으로만 참조 (순환 import 방지)
    from trader.core import Core

_DASHBOARD_REFRESH_SEC = 10.0  # 대시보드 편집 주기
_COLOR_LIVE = 0x2E7D32
_COLOR_IDLE = 0x616161
_COLOR_WARN = 0xEF6C00  # 감시 중인데 키움이 끊긴 위험 상태


class BotConfigError(Exception):
    """봇 설정이 없거나 불완전함 (봇 없이 실행하기 위한 신호)."""


@dataclass(frozen=True)
class BotConfig:
    token: str
    channel_id: int
    allowed_users: "frozenset[int]"

    def allows(self, user_id: int) -> bool:
        """화이트리스트가 비어 있으면 아무도 명령을 쓸 수 없다 (안전한 기본값)."""
        return user_id in self.allowed_users


def load_bot_config(config_path="config.toml") -> BotConfig:
    path = Path(config_path)
    if not path.exists():
        raise BotConfigError(f"{config_path} 가 없습니다.")
    section = tomllib.loads(path.read_text(encoding="utf-8")).get("discord", {})
    token = str(section.get("bot_token", "")).strip()
    channel = str(section.get("channel_id", "")).strip()
    if not token or not channel:
        raise BotConfigError("[discord] bot_token / channel_id 가 필요합니다.")
    try:
        channel_id = int(channel)
    except ValueError as e:
        raise BotConfigError(f"channel_id 가 숫자가 아닙니다: {channel}") from e
    users = set()
    for raw in section.get("allowed_users", []) or []:
        try:
            users.add(int(str(raw).strip()))
        except ValueError:
            continue
    if not users:
        raise BotConfigError(
            "[discord] allowed_users 에 본인 사용자 ID 를 넣어야 합니다 "
            "(비우면 아무도 명령을 쓸 수 없습니다)."
        )
    return BotConfig(token, channel_id, frozenset(users))


# ── 표시용 데이터 구성 (순수 함수 — 코어 없이 테스트 가능) ─────


def build_status_lines(entries: dict) -> list[str]:
    """보유·보류 종목 요약 줄. 대기 중인 종목은 생략한다."""
    from trader.state_machine import State

    lines = []
    for symbol, e in sorted(entries.items(), key=lambda kv: kv[1]["name"]):
        pos, price = e["pos"], e.get("price") or 0.0
        if pos.state is State.CLOSED and pos.total_bought:
            net = pos.realized_pnl - pos.fees
            lines.append(f"⚪ **{e['name']}({symbol})** 종료 · 세후 {net:+,.0f}원")
            continue
        if not pos.total_bought:
            continue
        icon = "🟢" if price >= pos.avg_price else "🔴"
        rate = (
            (price - pos.avg_price) / pos.avg_price if pos.avg_price and price else 0.0
        )
        lines.append(
            f"{icon} **{e['name']}({symbol})** {pos.state.value}\n"
            f"　평단 {pos.avg_price:,.0f} → {price:,.0f} ({rate:+.2%}) · "
            f"잔량 {pos.remaining}/{pos.total_bought}"
        )
    return lines


def build_dashboard_embed(
    core: "Core", blocked: "dict[str, str] | None" = None
) -> dict:
    """장중 대시보드 — 편집으로 갱신되는 단일 메시지."""
    from trader.state_machine import State

    entries = core.entries
    lines = build_status_lines(entries)
    for symbol, reason in (blocked or {}).items():
        if symbol in entries:
            lines.append(f"⏸️ **{entries[symbol]['name']}({symbol})** 보류 — {reason}")

    traded = [e["pos"] for e in entries.values() if e["pos"].total_bought]
    realized = sum(p.realized_pnl - p.fees for p in traded)
    unrealized = sum(
        (entries[s].get("price") or p.avg_price) * p.remaining
        - p.avg_price * p.remaining
        for s, e in entries.items()
        if (p := e["pos"]).remaining
    )
    holding = sum(1 for p in traded if p.state is not State.CLOSED and p.remaining)

    # 키움이 끊겨 있으면 감시 중이어도 매매가 되지 않는다 — 제목에서 바로 보이게 한다
    if not core.kiwoom_connected:
        lines.insert(0, "⛔ **키움 연결 안 됨** — 시세·주문이 동작하지 않습니다")

    # 계좌 기준 값이 있으면 그것을 쓴다 — 프로그램 밖에서 산 종목까지 포함되기 때문이다
    account = getattr(core, "account", {}) or {}
    footer = f"실현 {realized:+,.0f}원"
    if account.get("value"):
        footer += (
            f" · 평가 {account['value']:,.0f}원"
            f"({account.get('pnl', 0):+,.0f} · {account.get('rate', 0):+.2f}%)"
        )
    elif holding:
        footer += f" · 평가손익 {unrealized:+,.0f}원"
    if core.deposit_display is not None:
        footer += f" · 주문가능 {core.deposit_display:,.0f}원"
    if account.get("asset"):
        footer += f" · 자산 {account['asset']:,.0f}원"

    return {
        "title": f"📊 {core.trade_date} · "
        f"{'실전' if core.mode_real else '모의'} · "
        + (f"감시 중 ({len(entries)}종목)" if core.running else "감시 중지"),
        "description": "\n".join(lines) if lines else "보유 중인 종목이 없습니다.",
        "color": (
            _COLOR_LIVE
            if core.running and core.kiwoom_connected
            else (_COLOR_WARN if core.running else _COLOR_IDLE)
        ),
        "footer": {"text": f"{footer}\n갱신 {datetime.now():%H:%M:%S}"},
    }


# ── 봇 본체 ───────────────────────────────────────────────────


class TraderBot:
    """코어에 붙는 Discord 봇. discord.py 는 이 클래스 안에서만 import 한다."""

    def __init__(self, core: "Core", config: BotConfig):
        self._core = core
        self._config = config
        self._client = None
        self._channel = None
        self._dashboard_id = None
        self._blocked = {}
        self._pin_warned = False

    # 코어가 보류 상태를 알려주면 대시보드에 반영한다
    def set_blocked(self, symbol: str, active: bool, reason: str = "") -> None:
        if active:
            self._blocked[symbol] = reason
        else:
            self._blocked.pop(symbol, None)

    async def run(self) -> None:
        """봇 실행 (코어 루프의 태스크). 예외는 호출부가 로그로 남긴다."""
        intents = discord.Intents.none()
        intents.guilds = True  # 채널 조회에 필요 (메시지 내용 권한은 불필요)
        client = discord.Client(intents=intents)
        tree = app_commands.CommandTree(client)
        self._client = client
        self._register_commands(tree)

        @client.event
        async def on_ready() -> None:  # noqa: RUF029 — discord.py 규약
            self._channel = client.get_channel(
                self._config.channel_id
            ) or await client.fetch_channel(self._config.channel_id)
            await tree.sync()
            self._core.on_bot_ready()

        await client.start(self._config.token)

    async def close(self) -> None:
        await self.clear_dashboard()
        if self._client is not None:
            await self._client.close()

    # ── 발송 (알림·차트) ───────────────────────────────────────
    # discord.py 가 자체 큐로 레이트 리밋을 처리하므로 별도 간격 제어가 필요 없다.
    # 채널이 아직 준비되지 않았으면(연결 전·재연결 중) 조용히 버린다 —
    # 모든 알림은 화면 로그와 DB 에도 남으므로 유실돼도 이력은 보존된다.

    @property
    def ready(self) -> bool:
        return self._channel is not None

    async def send_text(self, text: str) -> bool:
        if self._channel is None:
            return False
        await self._channel.send(text[:1900])
        return True

    async def send_embed(self, data: dict) -> bool:
        if self._channel is None:
            return False
        await self._channel.send(embed=self._to_embed(data))
        return True

    async def send_images(self, paths: list[str], caption: str = "") -> bool:
        """이미지 여러 장을 한 메시지로 (복기 차트 일봉·3분봉)."""
        if self._channel is None:
            return False
        files = [discord.File(path) for path in paths]
        await self._channel.send(content=caption[:1900] or None, files=files)
        return True

    # ── 대시보드 ───────────────────────────────────────────────

    async def refresh_dashboard(self) -> None:
        """감시 중이면 대시보드를 만들거나 갱신한다."""
        if self._channel is None or not self._core.running:
            return
        embed = self._to_embed(build_dashboard_embed(self._core, self._blocked))
        if self._dashboard_id is None:
            message = await self._channel.send(embed=embed)
            self._dashboard_id = message.id
            try:
                await message.pin()
            except Exception as e:  # noqa: BLE001 — 고정에 실패해도 대시보드는 동작한다
                if not self._pin_warned:  # 원인을 한 번은 알려준다 (대개 권한 부족)
                    self._pin_warned = True
                    self._core.on_bot_warning(
                        f"대시보드 고정 실패 — 채널에 '메시지 관리' 권한이 필요합니다 ({e})"
                    )
            return
        try:
            message = await self._channel.fetch_message(self._dashboard_id)
            await message.edit(embed=embed)
        except Exception:  # noqa: BLE001 — 지워졌으면 다음 주기에 새로 만든다
            self._dashboard_id = None

    async def clear_dashboard(self) -> None:
        """장 마감·요약 발송 후 대시보드를 걷어낸다 (고정 해제 + 삭제)."""
        if self._channel is None or self._dashboard_id is None:
            return
        message_id, self._dashboard_id = self._dashboard_id, None
        try:
            message = await self._channel.fetch_message(message_id)
            await message.unpin()
            await message.delete()
        except Exception:  # noqa: BLE001 — 이미 없으면 그만
            pass

    def _to_embed(self, data: dict):
        embed = discord.Embed(
            title=data.get("title", ""),
            description=data.get("description", ""),
            color=data.get("color", 0),
        )
        for field in data.get("fields", []):
            embed.add_field(
                name=field["name"],
                value=field["value"],
                inline=field.get("inline", False),
            )
        if footer := data.get("footer", {}).get("text"):
            embed.set_footer(text=footer)
        return embed

    # ── 슬래시 명령 ────────────────────────────────────────────

    @staticmethod
    async def _settled(check, timeout: float = 3.0) -> bool:
        """명령이 실제로 반영될 때까지 잠깐 기다린다.

        봇은 상태를 직접 바꾸지 않고 명령 큐에 넣으므로(UI 와 같은 경로), 곧바로
        "요청했습니다" 라고만 답하면 코어가 거부해도 사용자는 알 수 없다.
        """
        import asyncio as _asyncio

        deadline = _asyncio.get_running_loop().time() + timeout
        while _asyncio.get_running_loop().time() < deadline:
            if check():
                return True
            await _asyncio.sleep(0.1)
        return False

    def _register_commands(self, tree) -> None:
        core, config = self._core, self._config

        async def guard(interaction) -> bool:
            """화이트리스트 + 채널 확인. 통과하지 못하면 조용히 거절한다."""
            if not config.allows(interaction.user.id):
                await interaction.response.send_message(
                    "이 봇을 사용할 권한이 없습니다.", ephemeral=True
                )
                return False
            if interaction.channel_id != config.channel_id:
                await interaction.response.send_message(
                    "지정된 채널에서만 사용할 수 있습니다.", ephemeral=True
                )
                return False
            return True

        @tree.command(name="상태", description="보유 종목과 손익을 조회합니다")
        async def status(interaction) -> None:
            if not await guard(interaction):
                return
            await interaction.response.defer()
            await core.refresh_display()  # 감시 중지 후에도 최신 금액을 보여준다
            await interaction.followup.send(
                embed=self._to_embed(build_dashboard_embed(core, self._blocked))
            )

        @tree.command(
            name="주문가능", description="지금 더 매수할 수 있는 금액을 조회합니다"
        )
        async def deposit(interaction) -> None:
            if not await guard(interaction):
                return
            await interaction.response.defer()
            try:
                value = await core.fetch_deposit()
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"조회 실패: {e}")
                return
            await interaction.followup.send(
                f"💰 주문가능금액 **{value:,.0f}원**\n"
                "-# 현금 + 당일 매도대금 재사용분 (영웅문 [예수금] 탭과 다른 값입니다)"
            )

        @tree.command(
            name="요약", description="매매 요약을 봅니다 (날짜를 비우면 오늘)"
        )
        @app_commands.describe(날짜="YYYY-MM-DD (비우면 오늘)")
        async def summary(interaction, 날짜: str = "") -> None:
            if not await guard(interaction):
                return
            await interaction.response.defer()
            try:
                embed = await core.summary_embed(날짜.strip())
            except ValueError:
                await interaction.followup.send(
                    "날짜는 YYYY-MM-DD 형식으로 입력하세요."
                )
                return
            await interaction.followup.send(embed=self._to_embed(embed))

        @summary.autocomplete("날짜")
        async def summary_autocomplete(interaction, current: str):
            """기록이 있는 매매일만 고르게 한다 — 빈 날짜를 조회할 일이 없다."""
            if not config.allows(interaction.user.id):
                return []
            text = (current or "").strip()
            return [
                app_commands.Choice(name=d, value=d)
                for d in core.trade_dates(25)
                if text in d
            ][:25]

        @tree.command(
            name="관심종목", description="오늘 관심종목의 태그·메모·기준봉을 봅니다"
        )
        @app_commands.describe(태그="특정 태그만 보기 (선택)", 쪽="페이지 (기본 1)")
        async def watchlist(interaction, 태그: str = "", 쪽: int = 1) -> None:
            if not await guard(interaction):
                return
            await interaction.response.send_message(
                embed=self._to_embed(
                    core.watchlist_embed(page=쪽, tag=태그.lstrip("#"))
                )
            )

        @watchlist.autocomplete("태그")
        async def watchlist_autocomplete(interaction, current: str):
            """실제로 쓰인 태그만 후보로 — 오타로 빈 결과가 나오지 않게 한다."""
            if not config.allows(interaction.user.id):
                return []
            text = (current or "").strip().lstrip("#")
            used: dict[str, int] = {}
            for e in core.entries.values():
                for tag in (
                    t.strip() for t in (e.get("tags") or "").split(",") if t.strip()
                ):
                    used[tag] = used.get(tag, 0) + 1
            return [
                app_commands.Choice(name=f"#{t} ({n}종목)", value=t)
                for t, n in sorted(used.items(), key=lambda x: -x[1])
                if text in t
            ][:25]

        @tree.command(
            name="근접도", description="미진입 종목이 1선에 얼마나 가까운지 봅니다"
        )
        async def proximity(interaction) -> None:
            if not await guard(interaction):
                return
            await interaction.response.send_message(
                embed=self._to_embed(core.proximity_embed())
            )

        @tree.command(name="차트", description="복기 차트를 생성해 이 채널로 보냅니다")
        @app_commands.describe(
            종목="종목코드 또는 종목명 (일부만 입력해도 후보가 뜹니다)"
        )
        async def chart(interaction, 종목: str) -> None:
            if not await guard(interaction):
                return
            symbol = core.find_symbol(종목)
            if symbol is None:
                await interaction.response.send_message(
                    f"'{종목}' 에 해당하는 관심종목이 없습니다.", ephemeral=True
                )
                return
            if not core.kiwoom_connected:
                await interaction.response.send_message(
                    "키움이 연결되어 있지 않아 차트를 만들 수 없습니다.", ephemeral=True
                )
                return
            name = core.entries[symbol]["name"]
            core.request_chart(
                symbol, to_discord=True
            )  # 명령을 낸 곳(Discord)으로 전송
            await interaction.response.send_message(
                f"📈 {name}({symbol}) 차트를 생성합니다 — 잠시 후 이 채널에 올라옵니다.",
                ephemeral=True,
            )

        @chart.autocomplete("종목")
        async def chart_autocomplete(interaction, current: str):
            """관심종목이 수십 개라 코드를 외우기 어렵다 — 입력하면서 고르게 한다."""
            if not config.allows(interaction.user.id):
                return []
            text = (current or "").strip().lower()
            hits = [
                (s, e["name"])
                for s, e in core.entries.items()
                if not text or text in s.lower() or text in e["name"].lower()
            ]
            return [
                app_commands.Choice(name=f"{name} ({sym})", value=sym)
                for sym, name in sorted(hits, key=lambda x: x[1])[:25]
            ]

        @tree.command(name="감시", description="감시를 시작하거나 중지합니다")
        @app_commands.describe(동작="시작 또는 중지")
        @app_commands.choices(
            동작=[
                app_commands.Choice(name="시작", value="start"),
                app_commands.Choice(name="중지", value="stop"),
            ]
        )
        async def watch(interaction, 동작: app_commands.Choice[str]) -> None:
            if not await guard(interaction):
                return
            want = 동작.value == "start"
            if want and not core.kiwoom_connected:
                await interaction.response.send_message(
                    "⛔ 키움이 연결되어 있지 않아 감시를 시작할 수 없습니다.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            core.request_running(want)
            ok = await self._settled(lambda: core.running == want)
            label = "시작" if want else "중지"
            await interaction.followup.send(
                f"👁️ 감시 {label} 완료 ({len(core.entries)}종목)"
                if ok
                else f"⚠️ 감시 {label} 요청이 반영되지 않았습니다 — 프로그램 로그를 확인하세요"
            )

        @tree.command(name="알림", description="알림 수준을 변경합니다")
        @app_commands.describe(수준="발송할 알림의 범위")
        @app_commands.choices(
            수준=[
                app_commands.Choice(name=level, value=level)
                for level in ("전체", "매매만 (시스템 제외)", "에러만", "끔")
            ]
        )
        async def notify(interaction, 수준: app_commands.Choice[str]) -> None:
            if not await guard(interaction):
                return
            await interaction.response.defer()
            core.request_notify_level(수준.value)
            ok = await self._settled(lambda: core.notify_level == 수준.value)
            await interaction.followup.send(
                f"🔔 알림 수준: **{수준.value}**"
                if ok
                else "⚠️ 알림 수준 변경이 반영되지 않았습니다"
            )
