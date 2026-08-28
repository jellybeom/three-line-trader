"""Discord 스레드 답글 ↔ 매매일지 (3단계-3).

순수 함수만 둔다 — Discord 없이 시험할 수 있고, 되먹임을 막는 판단도 여기서 한다.

입력 규칙
---------
스레드에 평소 채팅하듯 답글을 달면 된다. 슬래시 명령도 버튼도 쓰지 않는다 — 그것들은
봇이 켜져 있어야만 동작해서, 미니PC 가 꺼져 있으면 "상호작용에 실패했습니다" 가 뜬다.
일반 답글은 봇이 죽어 있어도 Discord 서버에 남아 있다가 다음 기동 때 들어온다.

    호가가 얇아 슬리피지 컸음        ← 접두어 없음 → 아쉬운 점
    + 손절 규칙 그대로 지킴          ← 잘한 점
    물타기 유혹 참음                 ← 앞의 '+' 가 이어져 잘한 점

**접두어를 만나면 칸이 바뀌고 다음 접두어까지 이어진다.** 접두어가 하나도 없는 답글은
통째로 아쉬운 점이다 — 복기에서 그냥 떠오르는 건 대개 아쉬운 쪽이라 그걸 기본으로 두면
아무것도 안 쳐도 된다. 잘한 점만 `+` 한 글자를 앞에 붙인다.

`-` 는 접두어로 쓰지 않는다. Discord 에서 줄 앞의 `-` 는 글머리 기호로 렌더링돼
화면이 지저분해진다.

되먹임 방지
-----------
UI 에서 쓴 일지는 봇이 스레드에 **자기 메시지 하나로 올려 두고 그것을 고쳐 쓴다**(B 방식).
그 메시지를 다시 읽어 DB 에 쓰면 무한히 돈다. 막는 층이 셋이다.

1. **작성자가 봇인 메시지는 읽지 않는다** — 이것만으로 고리가 끊긴다
2. `journal_sync.mirror_message_id` 와 대조해 한 번 더 거른다
3. 본문 첫 줄의 표식(`_MIRROR_MARK`)으로 마지막까지 거른다

세 층을 두는 이유는, 어느 하나가 어긋나도(봇 토큰 교체로 작성자 판정이 바뀌거나, DB 가
지워지거나) 조용히 무한 루프가 도는 대신 멈추게 하기 위해서다.
"""

from __future__ import annotations

_GOOD = "잘한 점"
_BAD = "아쉬운 점"

# 사람이 칠 접두어. 짧은 것(`+`)이 기본이고 긴 형태도 받아 준다.
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("+", _GOOD),
    ("잘한 점:", _GOOD),
    ("잘한점:", _GOOD),
    ("아쉬운 점:", _BAD),
    ("아쉬운점:", _BAD),
)

# UI 에서 쓴 일지를 봇이 스레드에 올릴 때 첫 줄에 붙이는 표식.
_MIRROR_MARK = "📝 프로그램에서 작성"


def _classify(line: str) -> tuple[str | None, str]:
    """(바뀔 칸, 남은 본문). 접두어가 없으면 (None, 원문)."""
    stripped = line.strip()
    for prefix, section in _PREFIXES:
        if stripped.startswith(prefix):
            return section, stripped[len(prefix) :].strip()
    return None, stripped


def parse_reply(text: str) -> tuple[str, str]:
    """답글 하나 → (잘한 점, 아쉬운 점).

    접두어를 만나면 칸이 바뀌고 다음 접두어까지 이어진다. 접두어가 하나도 없으면
    통째로 아쉬운 점이다.
    """
    sections: dict[str, list[str]] = {_GOOD: [], _BAD: []}
    current = _BAD
    for line in text.splitlines():
        section, body = _classify(line)
        if section is not None:
            current = section
        if body:
            sections[current].append(body)
    return "\n".join(sections[_GOOD]), "\n".join(sections[_BAD])


def collect_replies(texts: list[str]) -> tuple[str, str]:
    """답글 여러 개 → (잘한 점, 아쉬운 점). 스레드 순서대로 이어 붙인다.

    매번 **스레드 전체를 다시 읽어 처음부터 만든다.** 새 답글만 주워 담으면 수정과
    삭제가 반영되지 않는다. 전체를 다시 읽으면 몇 번을 돌려도 결과가 같아서, 사용자는
    Discord 의 기본 편집·삭제 기능을 그대로 쓰면 된다.
    """
    goods, bads = [], []
    for text in texts:
        good, bad = parse_reply(text)
        if good:
            goods.append(good)
        if bad:
            bads.append(bad)
    return "\n".join(goods), "\n".join(bads)


def is_mirror(text: str) -> bool:
    """봇이 UI 내용을 올려 둔 메시지인가. 되먹임 방지의 마지막 층이다."""
    return text.lstrip().startswith(_MIRROR_MARK)


def strip_mirror_mark(text: str) -> str:
    """봇이 올린 메시지에서 표식 줄만 떼어 낸다. 나머지는 답글과 같은 문법이다."""
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith(_MIRROR_MARK):
        lines = lines[1:]
    return "\n".join(lines)


def render_mirror(good: str, bad: str) -> str:
    """UI 에서 쓴 일지를 스레드에 올릴 본문.

    사람이 답글로 이어서 고칠 수 있도록 **입력 규칙과 같은 모양**으로 적는다. 그대로
    복사해 답글로 붙여도 같게 해석된다.
    """
    lines = [_MIRROR_MARK]
    if good:
        lines += [
            f"+ {line}" if i == 0 else line for i, line in enumerate(good.splitlines())
        ]
    if bad:
        lines += [
            f"{_BAD}: {line}" if i == 0 else line
            for i, line in enumerate(bad.splitlines())
        ]
    return "\n".join(lines)


def thread_name(trade_date: str, name: str, result: str) -> str:
    """`08-24 데이타솔루션 손절`.

    Discord 스레드 목록은 이름이 아니라 **최근 활동 순**으로 정렬되므로 앞의 날짜가
    정렬을 바꾸지는 않는다. 그래도 앞에 두면 목록에서 세로로 줄이 맞고 `08-24` 로
    검색하면 그날 것만 나온다. 종목코드는 넣지 않는다 — 스레드 첫 메시지(종료 알림)에
    이미 있어 검색에 걸리고, 제목에 또 넣으면 폰에서 잘리기만 한다.
    """
    return f"{trade_date[5:]} {name} {result}".strip()[:100]  # Discord 제한 100자
