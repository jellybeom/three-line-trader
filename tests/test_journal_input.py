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
    store.save_mirror("2026-08-24", "263800", "999", at="2000-01-01 00:00:00")

    pending = store.pending_mirrors()
    assert len(pending) == 1
    assert pending[0]["symbol"] == "263800"
    assert pending[0]["mirror_message_id"] == "999"  # 새로 올리지 않고 고쳐 쓴다
    assert pending[0]["good"] == "규칙 지킴"

    store.save_mirror("2026-08-24", "263800", "999", at="2099-01-01 00:00:00")
    assert store.pending_mirrors() == []  # 이미 올렸다


def test_아직_한_번도_안_올렸으면_밀린_것으로_잡힌다(store):
    """mirrored_at 이 빈 문자열이라 어떤 시각보다도 작다."""
    store.save_thread("2026-08-24", "263800", "111")
    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "")

    assert [p["symbol"] for p in store.pending_mirrors()] == ["263800"]


def test_같은_초에_저장하고_반영해도_놓치지_않는다(store):
    """시각이 초 단위다. `>` 로 비교하면 그 수정은 영영 안 올라간다.

    한 번 더 올리는 쪽은 같은 내용을 덮어쓸 뿐이라 해가 없다.
    """
    store.save_thread("2026-08-24", "263800", "111")
    store.replace_journal_text("2026-08-24", "263800", "규칙 지킴", "")
    same = store.journal_text("2026-08-24", "263800")[2]
    store.save_mirror("2026-08-24", "263800", "999", at=same)

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
