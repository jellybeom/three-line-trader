"""Discord 스레드 답글 ↔ 매매일지 (3단계-3).

파싱은 순수 함수라 Discord 없이 시험한다. 되먹임 방지는 세 층이라 층마다 따로 본다.
"""

from __future__ import annotations

import pytest

from trader.journal_input import (
    collect_replies,
    is_mirror,
    parse_reply,
    render_mirror,
    thread_name,
)
from trader.store import Store

# ── 접두어 규칙 ─────────────────────────────────────────────────


def test_접두어가_없으면_통째로_아쉬운_점이다():
    """복기에서 그냥 떠오르는 건 대개 아쉬운 쪽이다 — 그래야 아무것도 안 쳐도 된다."""
    good, bad = parse_reply("2선이 1선이랑 너무 가까웠음")
    assert good == ""
    assert bad == "2선이 1선이랑 너무 가까웠음"


def test_더하기_한_글자로_잘한_점을_적는다():
    """`잘한 점:` 을 매번 치는 건 번거롭다."""
    good, bad = parse_reply("+ 손절 규칙 그대로 지킴")
    assert good == "손절 규칙 그대로 지킴"
    assert bad == ""


def test_긴_접두어도_받는다():
    for text in ("잘한 점: 규칙 지킴", "잘한점: 규칙 지킴"):
        assert parse_reply(text) == ("규칙 지킴", "")
    for text in ("아쉬운 점: 늦었음", "아쉬운점: 늦었음"):
        assert parse_reply(text) == ("", "늦었음")


def test_한_답글에_둘_다_쓸_수_있다():
    """접두어를 만나면 칸이 바뀌고 다음 접두어까지 이어진다."""
    good, bad = parse_reply("호가가 얇아 슬리피지 컸음\n+ 손절 규칙 그대로 지킴")
    assert good == "손절 규칙 그대로 지킴"
    assert bad == "호가가 얇아 슬리피지 컸음"


def test_접두어_다음_줄은_같은_칸에_이어진다():
    good, bad = parse_reply(
        "+ 손절 규칙 그대로 지킴\n물타기 유혹 참음\n아쉬운 점: 2선이 가까웠음\n호가도 얇았음"
    )
    assert good == "손절 규칙 그대로 지킴\n물타기 유혹 참음"
    assert bad == "2선이 가까웠음\n호가도 얇았음"


def test_순서는_상관없다():
    a = parse_reply("아쉬운 점: 늦었음\n+ 규칙 지킴")
    b = parse_reply("+ 규칙 지킴\n아쉬운 점: 늦었음")
    assert a == b == ("규칙 지킴", "늦었음")


def test_빈_줄과_공백은_버린다():
    good, bad = parse_reply("  \n+ 규칙 지킴\n\n   \n")
    assert good == "규칙 지킴"
    assert bad == ""


def test_접두어만_있고_내용이_없으면_아무것도_넣지_않는다():
    assert parse_reply("+") == ("", "")
    assert parse_reply("아쉬운 점:") == ("", "")


def test_빼기는_접두어가_아니다():
    """Discord 에서 줄 앞의 `-` 는 글머리 기호로 렌더링돼 화면이 지저분해진다."""
    good, bad = parse_reply("- 호가가 얇았음")
    assert good == ""
    assert bad == "- 호가가 얇았음"


# ── 답글 여러 개 ────────────────────────────────────────────────


def test_답글_여러_개를_순서대로_이어_붙인다():
    good, bad = collect_replies(["호가가 얇았음", "+ 규칙 지킴", "2선이 가까웠음"])
    assert good == "규칙 지킴"
    assert bad == "호가가 얇았음\n2선이 가까웠음"


def test_전부_지우면_일지도_빈다():
    """스레드 전체를 다시 읽어 만들기 때문에 삭제가 그대로 반영된다."""
    assert collect_replies([]) == ("", "")


def test_같은_답글을_다시_읽어도_결과가_같다():
    """새 답글만 주워 담으면 수정·삭제가 반영되지 않는다 — 매번 처음부터 만든다."""
    texts = ["호가가 얇았음", "+ 규칙 지킴"]
    assert collect_replies(texts) == collect_replies(texts)


# ── 되먹임 방지 ─────────────────────────────────────────────────


def test_봇이_올린_메시지를_알아본다():
    """UI 내용을 스레드에 올린 것을 다시 읽어 DB 에 쓰면 무한히 돈다."""
    body = render_mirror("규칙 지킴", "2선이 가까웠음")
    assert is_mirror(body)
    assert not is_mirror("+ 규칙 지킴")
    assert not is_mirror("호가가 얇았음")


def test_올린_본문은_그대로_다시_읽어도_같은_뜻이다():
    """사람이 그 내용을 복사해 답글로 붙여도 같게 해석돼야 한다."""
    good, bad = "규칙 지킴\n물타기 참음", "2선이 가까웠음"
    body = render_mirror(good, bad)

    without_mark = "\n".join(body.splitlines()[1:])
    assert parse_reply(without_mark) == (good, bad)


def test_한쪽만_있어도_올릴_본문이_말이_된다():
    assert "아쉬운 점" not in render_mirror("규칙 지킴", "")
    assert "+" not in render_mirror("", "늦었음")
    assert render_mirror("", "").strip() != ""  # 표식은 남는다


# ── 스레드 이름 ─────────────────────────────────────────────────


def test_스레드_이름은_날짜_종목_결과다():
    assert (
        thread_name("2026-08-24", "데이타솔루션", "손절") == "08-24 데이타솔루션 손절"
    )


def test_스레드_이름이_길어도_잘리지_않게_자른다():
    """Discord 스레드 이름은 100자 제한이다."""
    assert len(thread_name("2026-08-24", "가" * 200, "손절")) <= 100


# ── journal_sync (스키마 v13) ───────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_스레드는_매매당_하나만_기억한다(store):
    """재전송으로 스레드를 또 만들면 답글이 갈라져 어느 쪽이 일지인지 알 수 없다."""
    store.save_thread("2026-08-24", "263800", "111")
    store.save_thread("2026-08-24", "263800", "222")  # 나중 것은 무시

    assert store.thread_of("2026-08-24", "263800") == "111"


def test_스레드로_어느_매매인지_되짚는다(store):
    """답글이 왔을 때 무엇의 일지인지 알아야 한다."""
    store.save_thread("2026-08-24", "263800", "111")

    assert store.trade_of_thread("111") == ("2026-08-24", "263800")
    assert store.trade_of_thread("없음") is None


def test_일지_본문을_넘긴_값_그대로_덮어쓴다(store):
    """스레드에서 오는 값은 전체를 다시 읽어 만든 최종본이다 — 비면 비워야 한다."""
    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "늦었음")
    assert store.journal_text("2026-08-24", "263800")[:2] == ("규칙 지킴", "늦었음")

    store.replace_journal_text("2026-08-24", "263800", "", "")
    assert store.journal_text("2026-08-24", "263800")[:2] == ("", "")


def test_save_journal_은_빈_값으로_지우지_않는다(store):
    """차트 경로와 코멘트가 서로 다른 시점에 같은 행을 갱신하기 때문이다."""
    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "늦었음")
    store.save_journal("2026-08-24", "263800", daily_path="a.png")

    good, bad, _ = store.journal_text("2026-08-24", "263800")
    assert (good, bad) == ("규칙 지킴", "늦었음")  # 살아 있다


def test_봇이_꺼진_사이_UI에서_쓴_것이_밀린_목록에_잡힌다(store):
    """다음 기동 때 이것을 훑어 스레드에 올린다."""
    store.save_thread("2026-08-24", "263800", "111")
    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "")

    pending = store.pending_mirrors()
    assert len(pending) == 1
    assert pending[0]["symbol"] == "263800"
    assert pending[0]["good"] == "규칙 지킴"

    store.save_mirror("2026-08-24", "263800", "999", "규칙 지킴", "")
    assert store.pending_mirrors() == []  # 같은 내용이면 올릴 것이 없다


def test_내용이_바뀌면_다시_올릴_것으로_잡힌다(store):
    store.save_thread("2026-08-24", "263800", "111")
    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "")
    store.save_mirror("2026-08-24", "263800", "999", "규칙 지킴", "")

    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "늦었음")

    pending = store.pending_mirrors()
    assert len(pending) == 1
    assert pending[0]["mirror_message_id"] == "999"  # 새로 올리지 않고 고쳐 쓴다


def test_같은_초에_저장하고_반영해도_놓치지_않는다(store):
    """시각이 초 단위라 같은 초의 두 변경을 구분할 수 없다 — 그래서 지문으로 판정한다."""
    store.save_thread("2026-08-24", "263800", "111")
    store.replace_journal_text("2026-08-24", "263800", "처음", "")
    store.save_mirror("2026-08-24", "263800", "999", "처음", "")
    store.replace_journal_text(
        "2026-08-24", "263800", "고친 것", ""
    )  # 같은 초일 수 있다

    assert len(store.pending_mirrors()) == 1


def test_본문이_비어_있으면_올리지_않는다(store):
    """차트 경로만 저장돼도 journal 행이 생긴다 — 빈 일지를 스레드에 올릴 이유는 없다."""
    store.save_thread("2026-08-24", "263800", "111")
    store.save_journal("2026-08-24", "263800", daily_path="a.png")

    assert store.pending_mirrors() == []


def test_스레드가_없으면_밀린_목록에_잡히지_않는다(store):
    """올릴 곳이 없는데 올리려 들면 매번 실패만 쌓인다."""
    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "")
    assert store.pending_mirrors() == []


# ── 봇 배선 (되먹임·중복 스레드) ────────────────────────────────


class _FakeMessage:
    def __init__(self, mid, content, bot=False):
        self.id = mid
        self.content = content
        self.author = type("A", (), {"bot": bot})()
        self.edited = None

    async def edit(self, content):
        self.edited = content
        self.content = content


class _FakeThread:
    def __init__(self, tid, messages=None):
        self.id = tid
        self.messages = list(messages or [])
        self._next = 1000

    async def history(self, limit=200, oldest_first=True):
        for m in self.messages:
            yield m

    async def send(self, body):
        self._next += 1
        msg = _FakeMessage(self._next, body, bot=True)
        self.messages.append(msg)
        return msg

    async def fetch_message(self, mid):
        for m in self.messages:
            if m.id == int(mid):
                return m
        raise KeyError(mid)


class _FakeClient:
    def __init__(self, thread):
        self.thread = thread
        self.intents = type("I", (), {"message_content": True})()

    def get_channel(self, cid):
        return self.thread if int(cid) == self.thread.id else None


class _FakeCore:
    def __init__(self, store):
        self._store = store
        self.updates = []

    @property
    def store(self):
        return self._store

    def on_journal_updated(self, trade_date, symbol):
        self.updates.append((trade_date, symbol))


@pytest.fixture
def bot(store, monkeypatch):
    from trader.discord_bot import BotConfig, TraderBot

    thread = _FakeThread(111)
    config = BotConfig("t", 1, frozenset({1}), journal_channel_id=2)
    b = TraderBot(_FakeCore(store), config)
    b._client = _FakeClient(thread)
    store.save_thread("2026-08-24", "263800", "111")
    return b, thread, store


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_스레드_답글이_일지가_된다(bot):
    b, thread, store = bot
    thread.messages = [
        _FakeMessage(1, "호가가 얇았음"),
        _FakeMessage(2, "+ 손절 규칙 지킴"),
    ]

    _run(b._rebuild_journal("111"))

    good, bad, _ = store.journal_text("2026-08-24", "263800")
    assert good == "손절 규칙 지킴"
    assert bad == "호가가 얇았음"


def test_답글을_지우면_일지에서도_사라진다(bot):
    """스레드 전체를 다시 읽으므로 삭제가 그대로 반영된다."""
    b, thread, store = bot
    thread.messages = [_FakeMessage(1, "호가가 얇았음"), _FakeMessage(2, "+ 규칙 지킴")]
    _run(b._rebuild_journal("111"))

    thread.messages = [m for m in thread.messages if m.id != 2]
    _run(b._rebuild_journal("111"))

    good, bad, _ = store.journal_text("2026-08-24", "263800")
    assert good == ""  # 지운 것이 되살아나지 않는다
    assert bad == "호가가 얇았음"


def test_봇이_올린_메시지는_답글로_세지_않는다(bot):
    """되먹임 1층. 이것이 읽히면 UI 내용이 답글로 둔갑해 무한히 돈다."""
    b, thread, store = bot
    _run(b.mirror_journal("2026-08-24", "263800", "UI 에서 쓴 것", ""))
    thread.messages.append(_FakeMessage(1, "폰에서 쓴 것"))

    _run(b._rebuild_journal("111"))

    good, bad, _ = store.journal_text("2026-08-24", "263800")
    assert good == ""  # 봇 메시지가 '잘한 점' 으로 섞여 들어오지 않았다
    assert bad == "폰에서 쓴 것"


def test_봇_판정이_어긋나도_표식으로_걸러진다(bot):
    """되먹임 3층. 봇 토큰을 갈면 작성자 판정이 바뀔 수 있다."""
    from trader.journal_input import render_mirror

    b, thread, store = bot
    thread.messages = [
        _FakeMessage(1, render_mirror("UI 에서 쓴 것", ""), bot=False),  # 사람으로 보임
        _FakeMessage(2, "폰에서 쓴 것"),
    ]

    _run(b._rebuild_journal("111"))

    good, bad, _ = store.journal_text("2026-08-24", "263800")
    assert good == ""  # 봇이 올린 것은 무시
    assert bad == "폰에서 쓴 것"


def test_UI_반영은_같은_메시지를_고쳐_쓴다(bot):
    """매번 새로 올리면 스레드가 같은 내용으로 도배된다."""
    b, thread, store = bot
    _run(b.mirror_journal("2026-08-24", "263800", "처음", ""))
    first = store.mirror_of("2026-08-24", "263800")[0]

    _run(b.mirror_journal("2026-08-24", "263800", "고친 것", ""))

    assert store.mirror_of("2026-08-24", "263800")[0] == first  # 같은 메시지
    assert len([m for m in thread.messages if m.author.bot]) == 1
    assert "고친 것" in thread.messages[-1].content


def test_스레드에서_온_내용을_다시_스레드로_올리지_않는다(bot):
    """읽고 → 쓰고 → 그것을 다시 읽는 고리가 생기면 안 된다."""
    b, thread, store = bot
    thread.messages = [_FakeMessage(1, "폰에서 쓴 것")]

    _run(b._rebuild_journal("111"))

    assert store.pending_mirrors() == []  # 올릴 것으로 잡히지 않는다


def test_일지가_비면_스레드에_올리지_않는다(bot):
    b, thread, store = bot
    assert _run(b.mirror_journal("2026-08-24", "263800", "", "")) is False
    assert thread.messages == []


def test_스레드가_없으면_올리지_않는다(bot):
    b, thread, store = bot
    assert _run(b.mirror_journal("2026-08-24", "999999", "내용", "")) is False


def test_다른_사람의_다른_채널_메시지는_무시한다(bot):
    """일지 채널이 아닌 곳의 대화까지 읽으면 엉뚱한 것이 일지가 된다."""
    b, thread, store = bot
    message = _FakeMessage(1, "잡담")
    message.channel = type("C", (), {"id": 999})()  # 등록되지 않은 스레드

    _run(b._on_journal_change(message))

    assert store.journal_text("2026-08-24", "263800")[:2] == ("", "")


def test_답글이_없으면_UI에서_쓴_일지를_지우지_않는다(bot):
    """기동 시 밀린 것 훑기가 모든 스레드를 돈다 — 여기서 지우면 재시작마다 날아간다."""
    b, thread, store = bot
    store.replace_journal_text("2026-08-24", "263800", "UI 에서 쓴 것", "")
    _run(b.mirror_journal("2026-08-24", "263800", "UI 에서 쓴 것", ""))

    _run(b._rebuild_journal("111"))  # 봇 메시지만 있는 스레드

    assert store.journal_text("2026-08-24", "263800")[:2] == ("UI 에서 쓴 것", "")


def test_답글을_달면_그_스레드가_일지의_주인이_된다(bot):
    """둘을 합치면 UI 저장 때마다 답글이 접혀 들어가 중복된다 — 한쪽이 주인이어야 한다."""
    b, thread, store = bot
    store.replace_journal_text("2026-08-24", "263800", "UI 에서 쓴 것", "")
    _run(b.mirror_journal("2026-08-24", "263800", "UI 에서 쓴 것", ""))

    thread.messages.append(_FakeMessage(1, "폰에서 쓴 것"))
    _run(b._rebuild_journal("111"))

    good, bad, _ = store.journal_text("2026-08-24", "263800")
    assert (good, bad) == ("", "폰에서 쓴 것")


# ── 스레드 생성 · 기동 시 밀린 것 훑기 ──────────────────────────


class _FakeChannel:
    """매매일지 채널. send 하면 메시지가 생기고 거기서 스레드를 연다."""

    def __init__(self):
        self.sent = []
        self._next = 500

    async def send(self, embed=None):
        self._next += 1
        thread = _FakeThread(self._next)
        message = _FakeMessage(self._next, "")
        message.create_thread = lambda name, auto_archive_duration=None: _made(
            thread, name
        )
        self.sent.append((embed, thread))
        return message


async def _made(thread, name):
    thread.name = name
    return thread


def test_종료된_매매마다_스레드가_하나_생긴다(store):
    from trader.discord_bot import BotConfig, TraderBot

    b = TraderBot(_FakeCore(store), BotConfig("t", 1, frozenset({1}), 2))
    b._journal_channel = _FakeChannel()

    assert _run(
        b.open_journal_thread("2026-08-26", "263800", "데이타솔루션", "손절", {})
    )

    _, thread = b._journal_channel.sent[0]
    assert thread.name == "08-26 데이타솔루션 손절"
    assert store.thread_of("2026-08-26", "263800") == str(thread.id)


def test_같은_매매에_스레드를_두_번_만들지_않는다(store):
    """재시작·재전송으로 두 번 불려도 답글이 두 곳으로 갈라지면 안 된다."""
    from trader.discord_bot import BotConfig, TraderBot

    b = TraderBot(_FakeCore(store), BotConfig("t", 1, frozenset({1}), 2))
    b._journal_channel = _FakeChannel()
    _run(b.open_journal_thread("2026-08-26", "263800", "데이타솔루션", "손절", {}))
    first = store.thread_of("2026-08-26", "263800")

    assert not _run(
        b.open_journal_thread("2026-08-26", "263800", "데이타솔루션", "손절", {})
    )
    assert len(b._journal_channel.sent) == 1
    assert store.thread_of("2026-08-26", "263800") == first


def test_일지_채널이_없으면_스레드를_만들지_않는다(store):
    """journal_channel_id 를 비워 두면 스레드 기능만 꺼지고 나머지는 평소대로 돈다."""
    from trader.discord_bot import BotConfig, TraderBot

    b = TraderBot(_FakeCore(store), BotConfig("t", 1, frozenset({1})))
    assert not _run(b.open_journal_thread("2026-08-26", "263800", "종목", "손절", {}))


def test_기동할_때_봇이_꺼진_사이의_답글을_주워_담는다(bot):
    """폰에서 단 답글은 서버에 남아 있다가 다음 기동 때 들어온다."""
    b, thread, store = bot
    thread.messages = [_FakeMessage(1, "폰에서 쓴 것")]

    _run(b._collect_backlog())

    assert store.journal_text("2026-08-24", "263800")[:2] == ("", "폰에서 쓴 것")


def test_기동할_때_UI에서_쓴_것을_스레드에_올린다(bot):
    """봇이 꺼진 사이 UI 에서 저장하면 그때는 올릴 수 없다."""
    b, thread, store = bot
    store.replace_journal_text("2026-08-24", "263800", "UI 에서 쓴 것", "")

    _run(b._collect_backlog())

    assert any(m.author.bot for m in thread.messages)
    assert store.pending_mirrors() == []  # 두 번 올리지 않는다


def test_기동_훑기를_두_번_돌려도_결과가_같다(bot):
    """되먹임이 있으면 여기서 내용이 불어난다."""
    b, thread, store = bot
    thread.messages = [_FakeMessage(1, "폰에서 쓴 것")]

    _run(b._collect_backlog())
    first = store.journal_text("2026-08-24", "263800")[:2]
    _run(b._collect_backlog())

    assert store.journal_text("2026-08-24", "263800")[:2] == first
    assert len([m for m in thread.messages if m.author.bot]) <= 1


def test_인텐트가_꺼져_있으면_경고한다(bot):
    """안 켜면 답글 본문이 빈 문자열로 와서 일지가 조용히 비어 버린다."""
    b, thread, store = bot
    b._client.intents = type("I", (), {"message_content": False})()
    sent = []
    b.send_text = lambda text: _noop(sent, text)

    _run(b._warn_if_no_message_content())

    assert sent and "Message Content Intent" in sent[0]


async def _noop(sent, text):
    sent.append(text)
