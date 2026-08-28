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

from trader import journal_input
from trader.notifier import build_trade_embed

_BACKLOG_MAX = 30  # 기동 시 훑을 최근 스레드 수 (약 한 달치)
_BACKFILL_DAYS = 5  # 스레드를 뒤늦게 만들 대상 기간 (최근 매매일 수)

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
    # 매매일지 전용 채널. 비어 있으면(0) 스레드 기능만 꺼지고 나머지는 평소대로 돈다 —
    # 설정을 안 채운 상태로 업데이트해도 알림이 멈추면 안 된다.
    journal_channel_id: int = 0

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
    journal_channel = str(section.get("journal_channel_id", "")).strip()
    try:
        journal_channel_id = int(journal_channel) if journal_channel else 0
    except ValueError as e:
        raise BotConfigError(
            f"journal_channel_id 가 숫자가 아닙니다: {journal_channel}"
        ) from e
    if journal_channel_id and journal_channel_id == channel_id:
        raise BotConfigError(
            "journal_channel_id 가 channel_id 와 같습니다 — "
            "매매일지 스레드는 별도 채널이어야 알림과 섞이지 않습니다."
        )
    return BotConfig(token, channel_id, frozenset(users), journal_channel_id)


# ── 표시용 데이터 구성 (순수 함수 — 코어 없이 테스트 가능) ─────


def build_status_lines(entries: dict, holdings: dict | None = None) -> list[str]:
    """보유·보류 종목 요약 줄. 대기 중인 종목은 생략한다.

    holdings 는 {종목: 보유기간} — 폰으로 볼 때 "이거 며칠째 들고 있더라" 가
    평단·잔량만큼 자주 궁금한 값이라 잔량 옆에 붙인다.
    """
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
            + (f" · 보유 {held}" if (held := (holdings or {}).get(symbol)) else "")
        )
    return lines


def build_dashboard_embed(
    core: "Core", blocked: "dict[str, str] | None" = None
) -> dict:
    """장중 대시보드 — 편집으로 갱신되는 단일 메시지."""
    from trader.state_machine import State

    entries = core.entries
    lines = build_status_lines(entries, {s: core.holding_label(s) for s in entries})
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
        self._journal_channel = None
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
        intents.guilds = True  # 채널 조회에 필요
        if self._config.journal_channel_id:
            # 매매일지 답글을 읽으려면 둘 다 필요하다. message_content 는 **특권 인텐트**라
            # 개발자 포털에서 따로 켜야 한다 — 안 켜면 본문이 빈 문자열로 와서 일지가
            # 조용히 비어 버린다. on_ready 에서 확인해 경고한다.
            intents.guild_messages = True
            intents.message_content = True
        client = discord.Client(intents=intents)
        tree = app_commands.CommandTree(client)
        self._client = client
        self._register_commands(tree)

        @client.event
        async def on_ready() -> None:  # noqa: RUF029 — discord.py 규약
            self._channel = client.get_channel(
                self._config.channel_id
            ) or await client.fetch_channel(self._config.channel_id)
            if self._config.journal_channel_id:
                self._journal_channel = client.get_channel(
                    self._config.journal_channel_id
                ) or await client.fetch_channel(self._config.journal_channel_id)
                await self._warn_if_no_message_content()
                await self._warn_if_missing_permissions()
                await self._collect_backlog()
            await tree.sync()
            self._core.on_bot_ready()

        @client.event
        async def on_message(message) -> None:
            await self._on_journal_change(message)

        @client.event
        async def on_message_edit(_before, after) -> None:
            await self._on_journal_change(after)

        @client.event
        async def on_message_delete(message) -> None:
            await self._on_journal_change(message)

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

    async def send_images(
        self, paths: list[str], caption: str = "", thread_key: tuple | None = None
    ) -> bool:
        """이미지 여러 장을 한 메시지로 (복기 차트 일봉·3분봉).

        thread_key 가 (매매일, 종목) 이고 그 매매의 스레드가 있으면 **스레드 안으로**
        보낸다. 종료 차트는 복기할 때 보는 것이라, 답글을 다는 자리 바로 위에 있어야
        채널을 오가지 않는다. 스레드가 없으면(일지 채널 미설정 · 생성 실패) 알림
        채널로 물러난다 — 차트가 아예 사라지는 것보다 낫다.

        스레드로 보낸 차트는 작성자가 봇이라 답글로 세지 않는다 (되먹임 방지 1층).
        """
        channel = self._channel
        if thread_key and (thread := await self._thread_for(*thread_key)) is not None:
            channel = thread
        if channel is None:
            return False
        files = [discord.File(path) for path in paths]
        await channel.send(content=caption[:1900] or None, files=files)
        return True

    async def _thread_for(self, trade_date: str, symbol: str):
        """그 매매의 스레드 객체. 없거나 접근할 수 없으면 None."""
        thread_id = self._core.store.thread_of(trade_date, symbol)
        if not thread_id or self._client is None:
            return None
        try:
            return self._client.get_channel(
                int(thread_id)
            ) or await self._client.fetch_channel(int(thread_id))
        except (discord.HTTPException, ValueError):
            return None

    # ── 매매일지 스레드 (3단계-3) ───────────────────────────────
    #
    # Store 는 캐시하지 않고 매번 `self._core.store` 로 가져온다. 실전/모의 모드를 바꾸면
    # 코어가 Store 를 새로 만들기 때문에, 붙들고 있으면 옛 DB 에 쓰게 된다.
    #
    # 종료된 매매마다 스레드를 하나 열고, 사용자가 답글을 달면 일지가 된다. 슬래시
    # 명령이나 버튼은 쓰지 않는다 — 봇이 켜져 있어야만 동작해서 미니PC 가 꺼져 있으면
    # 실패한다. 일반 답글은 봇이 죽어 있어도 서버에 남아 있다가 다음 기동 때 들어온다.

    async def open_journal_thread(
        self, trade_date: str, symbol: str, name: str, result: str, embed: dict
    ) -> bool:
        """종료 알림을 매매일지 채널에 보내고 스레드를 연다.

        이미 스레드가 있으면 아무것도 하지 않는다 — 매매당 스레드는 하나여야 답글이
        갈라지지 않는다. 재시작·재전송으로 두 번 불려도 안전하다.
        """
        if self._journal_channel is None or self._core.store.thread_of(
            trade_date, symbol
        ):
            return False
        try:
            message = await self._journal_channel.send(embed=self._to_embed(embed))
        except discord.Forbidden as err:
            # 50001 Missing Access. 비공개 채널은 역할별로 접근을 따로 허용해야 한다 —
            # 채널은 보이는데 전송만 막혀 청산할 때마다 같은 오류가 난다(2026-08-26).
            raise BotConfigError(
                "매매일지 채널에 글을 쓸 권한이 없습니다. 채널 편집 → 권한에서 봇에게 "
                "'채널 보기 · 메시지 보내기 · 공개 스레드 만들기 · 스레드에서 메시지 "
                "보내기 · 메시지 기록 보기' 를 허용해 주세요."
            ) from err
        thread = await message.create_thread(
            name=journal_input.thread_name(trade_date, name, result),
            auto_archive_duration=10080,  # 7일. 그 뒤 답글을 달면 Discord 가 되살린다
        )
        self._core.store.save_thread(trade_date, symbol, str(thread.id))
        return True

    async def _on_journal_change(self, message) -> None:
        """스레드에 답글이 달리거나 고쳐지거나 지워지면 일지를 다시 만든다.

        **되먹임 방지 1층**: 작성자가 봇인 메시지는 무시한다. 봇이 UI 내용을 올려 둔
        메시지를 다시 읽어 DB 에 쓰면 무한히 돈다. 이 한 줄로 고리가 끊긴다.
        """
        if getattr(message.author, "bot", False):
            return
        thread_id = str(getattr(message.channel, "id", ""))
        if not self._core.store.trade_of_thread(thread_id):
            return
        await self._rebuild_journal(thread_id)

    async def _rebuild_journal(self, thread_id: str) -> None:
        """스레드 **전체를 다시 읽어** 일지를 처음부터 만든다.

        새 답글만 주워 담으면 수정과 삭제가 반영되지 않는다. 전체를 다시 읽으면 몇 번을
        돌려도 결과가 같아서, 사용자는 Discord 의 기본 편집·삭제를 그대로 쓰면 된다.

        **한 번이라도 답글을 달면 그 스레드가 일지의 주인이 된다.** 답글이 없는 동안은
        UI 에서 쓴 것이 그대로 남고, 첫 답글이 달리는 순간 UI 내용을 일지 **앞으로
        흡수**한 뒤 주인이 넘어간다. 흡수는 그 한 번뿐이라 중복될 일이 없다.

        둘을 계속 합치는 쪽은 택하지 않았다. UI 창에는 늘 '합쳐진 전체' 가 보이는데
        거기서 저장하면 답글 내용까지 봇 메시지로 들어가고, 다음에 읽을 때 같은 문장이
        두 번 세어진다. 막으려면 문장마다 출처를 추적해야 하고 그러면 자유롭게 고칠 수
        없게 된다 — 자유 서술로 정한 취지와 어긋난다.
        """
        trade = self._core.store.trade_of_thread(thread_id)
        if trade is None:
            return
        trade_date, symbol = trade
        mirror_id, _ = self._core.store.mirror_of(trade_date, symbol)
        thread = await self._thread_for(trade_date, symbol)
        if thread is None:
            return

        # 봇이 올린 UI 내용(mirror)과 사람이 단 답글을 나눠 담는다. 되먹임 방지 3층은
        # 그대로다 — mirror 를 '답글' 로 세지 않는 것이 핵심이고, 아래에서 **읽기 전용
        # 으로만** 앞에 붙인다.
        mirror_text, texts = "", []
        async for message in thread.history(limit=200, oldest_first=True):
            body = message.content or ""
            is_mirror = (mirror_id and str(message.id) == mirror_id) or (
                journal_input.is_mirror(body)
            )
            if is_mirror:
                mirror_text = body
                continue
            if getattr(message.author, "bot", False):
                continue  # 차트 등 봇이 올린 나머지
            texts.append(body)

        store = self._core.store
        if not texts and not store.has_replies(trade_date, symbol):
            # 답글이 **한 번도 없었으면** DB 를 건드리지 않는다. 봇이 올린 메시지만 남은
            # 스레드를 '전부 지웠다' 로 읽으면 UI 에서 쓴 일지가 지워진다 — 기동 시 밀린
            # 것 훑기가 모든 스레드를 돌기 때문에 재시작마다 날아간다(2026-08-25).
            return

        if texts:
            store.mark_replied(trade_date, symbol)
        # **일지 = 스레드에 보이는 그대로.** 봇이 올린 UI 내용이 맨 위, 그 뒤에 답글이
        # 순서대로다. UI 내용을 일지에만 옮겨 두면 다음 갱신에서 사라진다 — 스레드에
        # 답글로 없기 때문이다(2026-08-28 테스트에서 잡음). 스레드에 남아 있는 것을
        # 매번 그대로 읽으면 몇 번을 돌려도 결과가 같다.
        #
        # 답글이 달린 뒤로는 mirror_journal 이 이 메시지를 더 고치지 않으므로, 이 부분은
        # 넘어가는 시점의 UI 내용으로 고정된다. 중복될 일이 없다.
        good, bad = journal_input.collect_replies(
            ([journal_input.strip_mirror_mark(mirror_text)] if mirror_text else [])
            + texts
        )
        if (good, bad) == store.journal_text(trade_date, symbol)[:2]:
            # 내용이 그대로면 아무것도 하지 않는다. 기동할 때마다 스레드를 다시 읽으므로,
            # 여기서 걸러 내지 않으면 답글을 단 종목 수만큼 "일지가 갱신됐습니다" 로그가
            # 매번 쌓인다(2026-08-26 실측). 수정 시각도 괜히 밀려 밀린 목록 판정이
            # 흔들린다.
            return

        store.replace_journal_text(trade_date, symbol, good, bad)
        # 스레드와 DB 가 지금 같은 내용이라고 새긴다. 안 새기면 방금 읽어 온 내용을
        # 다시 스레드로 올리려 들고, 그것을 또 읽는 고리가 생긴다.
        store.save_mirror(trade_date, symbol, mirror_id, good, bad)
        self._core.on_journal_updated(trade_date, symbol)

    async def mirror_journal(
        self, trade_date: str, symbol: str, good: str, bad: str
    ) -> bool:
        """UI 에서 쓴 일지를 스레드에 올린다 (B 방식).

        **메시지를 하나 두고 그것을 고쳐 쓴다.** 저장할 때마다 새로 올리면 스레드가
        같은 내용으로 도배된다. 올린 메시지는 작성자가 봇이라 다시 읽히지 않는다.
        """
        thread_id = self._core.store.thread_of(trade_date, symbol)
        if self._client is None or not thread_id or not (good or bad):
            return False
        if self._core.store.has_replies(trade_date, symbol):
            # 답글이 달린 뒤로는 스레드가 일지의 주인이다. 계속 고쳐 쓰면 답글에서 읽은
            # 내용이 이 메시지로 되돌아와 같은 문장이 두 번 세어진다.
            return False
        body = journal_input.render_mirror(good, bad)
        mirror_id, _ = self._core.store.mirror_of(trade_date, symbol)
        thread = await self._thread_for(trade_date, symbol)
        if thread is None:
            return False
        try:
            if mirror_id:
                message = await thread.fetch_message(int(mirror_id))
                await message.edit(content=body[:1900])
            else:
                message = await thread.send(body[:1900])
                mirror_id = str(message.id)
        except (discord.HTTPException, ValueError):
            return False  # 다음 기동의 밀린 목록에서 다시 시도된다
        self._core.store.save_mirror(trade_date, symbol, mirror_id, good, bad)
        return True

    async def backfill_threads(self) -> int:
        """스레드가 없는 **최근** 청산 매매에 스레드를 만든다. 만든 개수를 돌려준다.

        청산 순간에 실패하면(채널 권한이 없거나 봇이 꺼져 있으면) 그 매매는 영영
        스레드가 없다. 기동할 때 메워 두면 권한을 고친 뒤 재시작만으로 복구된다.

        대상은 **최근 며칠**로 자른다 — 자세한 이유는 store.closed_without_thread 참고.
        """
        if self._journal_channel is None:
            return 0
        dates = self._core.store.recent_trade_dates(limit=_BACKFILL_DAYS)
        made = 0
        for row in self._core.store.closed_without_thread(
            since=dates[-1] if dates else ""
        ):
            net = (row["realized_pnl"] or 0) - (row["fees"] or 0)
            try:
                if await self.open_journal_thread(
                    row["trade_date"],
                    row["symbol"],
                    row["name"],
                    "익절" if net > 0 else "손절" if net < 0 else "본전",
                    self._core.journal_embed(row["trade_date"], row["symbol"]),
                ):
                    made += 1
                    await self._attach_charts(row["trade_date"], row["symbol"])
            except (discord.HTTPException, BotConfigError):
                break  # 권한이 없으면 나머지도 마찬가지다 — 같은 오류를 쌓지 않는다
        return made

    async def refresh_thread_embeds(self) -> int:
        """이미 만들어진 스레드의 첫 메시지를 최신 형식으로 고쳐 쓴다.

        스레드는 청산 순간에 한 번 만들어지고 그대로 남는다. embed 에 담는 내용을
        바꿔도 예전 스레드는 옛 모습 그대로라, 며칠 전 매매를 복기하려면 정보가 빠져
        있다. 메시지에서 스레드를 만들면 **스레드 ID 가 그 메시지 ID 와 같아서** 부모
        채널에서 바로 찾아 고칠 수 있다.

        내용이 같으면 건드리지 않는다 — 재시작할 때마다 편집 표시가 붙으면 지저분하다.
        """
        if self._journal_channel is None:
            return 0
        fixed = 0
        for trade_date, symbol in self._core.store.threads_with_replies()[
            :_BACKLOG_MAX
        ]:
            thread_id = self._core.store.thread_of(trade_date, symbol)
            embed = self._core.journal_embed(trade_date, symbol)
            if not embed:
                continue
            try:
                message = await self._journal_channel.fetch_message(int(thread_id))
                current = message.embeds[0].description if message.embeds else ""
                if current == embed["description"]:
                    continue
                await message.edit(embed=self._to_embed(embed))
                fixed += 1
                await self._attach_charts(trade_date, symbol)
            except (discord.HTTPException, ValueError, IndexError):
                continue
        return fixed

    async def _attach_charts(self, trade_date: str, symbol: str) -> None:
        """보관해 둔 복기 차트를 스레드에 올린다. 이미 올라가 있으면 건너뛴다."""
        paths = [p for p in self._core.store.chart_paths(trade_date, symbol) if p]
        thread = await self._thread_for(trade_date, symbol)
        if not paths or thread is None:
            return
        async for message in thread.history(limit=50, oldest_first=True):
            if getattr(message, "attachments", None):
                return  # 이미 있다
        try:
            await thread.send(files=[discord.File(p) for p in paths])
        except (discord.HTTPException, OSError):
            pass  # 차트가 없어도 복기는 된다

    async def _collect_backlog(self) -> None:
        """기동할 때 밀린 것을 양쪽으로 정리한다.

        봇이 꺼진 사이 폰에서 단 답글과, UI 에서 저장한 일지가 각각 밀려 있다. 답글을
        먼저 읽어야 한다 — 반대로 하면 UI 내용을 올린 뒤 그것을 답글로 착각해 읽는 순서
        문제가 생길 수 있다.
        """
        # 최근 것부터 일정 개수만 본다. 스레드가 수백 개가 되어도 기동이 늘어지면 안 된다.
        if made := await self.backfill_threads():
            await self.send_text(f"매매일지 스레드 {made}개를 만들었습니다.")
        await self.refresh_thread_embeds()
        for trade_date, symbol in self._core.store.threads_with_replies()[
            :_BACKLOG_MAX
        ]:
            try:
                await self._rebuild_journal(
                    self._core.store.thread_of(trade_date, symbol)
                )
            except discord.HTTPException:
                continue
        for row in self._core.store.pending_mirrors():
            try:
                await self.mirror_journal(
                    row["trade_date"], row["symbol"], row["good"], row["bad"]
                )
            except discord.HTTPException:
                continue

    _NEEDED = (
        ("view_channel", "채널 보기"),
        ("send_messages", "메시지 보내기"),
        ("create_public_threads", "공개 스레드 만들기"),
        ("send_messages_in_threads", "스레드에서 메시지 보내기"),
        ("read_message_history", "메시지 기록 보기"),
        ("embed_links", "링크 첨부"),
    )

    async def _warn_if_missing_permissions(self) -> None:
        """일지 채널 권한을 **시작할 때** 확인한다.

        없으면 청산할 때마다 `50001 Missing Access` 가 나는데, 그때는 이미 장중이라
        고치기 어렵고 그날 청산된 종목이 전부 스레드 없이 지나간다(2026-08-26 실측).
        비공개 채널은 역할별로 접근을 따로 허용해야 해서 흔히 빠뜨린다.
        """
        channel = self._journal_channel
        if channel is None or not hasattr(channel, "permissions_for"):
            return
        me = getattr(getattr(channel, "guild", None), "me", None)
        if me is None:
            return
        have = channel.permissions_for(me)
        missing = [
            label for attr, label in self._NEEDED if not getattr(have, attr, False)
        ]
        if missing:
            await self.send_text(
                f"⚠️ 매매일지 채널 권한이 부족합니다: {' · '.join(missing)}. "
                "채널 편집 → 권한에서 봇에게 허용해 주세요. "
                "그때까지 종료된 매매의 스레드가 만들어지지 않습니다."
            )

    async def _warn_if_no_message_content(self) -> None:
        """특권 인텐트가 꺼져 있으면 알린다.

        안 켜면 답글 본문이 **빈 문자열로 온다.** 그대로 두면 일지가 조용히 비워지므로
        시작할 때 한 번 크게 알린다.
        """
        if self._client is not None and self._client.intents.message_content:
            return
        await self.send_text(
            "⚠️ Message Content Intent 가 꺼져 있어 매매일지 답글을 읽을 수 없습니다. "
            "Discord 개발자 포털 → Bot → Privileged Gateway Intents 에서 켜 주세요."
        )

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
