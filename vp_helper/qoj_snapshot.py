"""QOJ's visible scoreboard, kept separate from verified original replays."""
from __future__ import annotations

import math

from .models import Cell, Contest, Reference, TeamScore
from .parsing import js_value
from .scoring import statistics


def reference_from_html(html: str, contest: Contest, *, released: bool = False,
                        elapsed: float | None = None) -> Reference:
    def unavailable(reason):
        return Reference(released=released, message="QOJ reference unavailable: " + reason)

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    raw_rows = js_value(html, "standings")
    raw_scores = js_value(html, "score")
    labels = js_value(html, "problems_id")
    if (not isinstance(raw_rows, list) or not isinstance(raw_scores, dict)
            or labels != list(contest.problems)):
        return unavailable("missing scoreboard data or mismatched problem labels.")
    if js_value(html, "standings_version", 1) not in (1, 2) or js_value(html, "icpc_partial_scores", False):
        return unavailable("unsupported scoreboard version or partial-score ICPC rules.")
    rows, seen = [], set()
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) < 3 or not isinstance(raw[2], list) or len(raw[2]) < 4:
            return unavailable("unrecognized team identity fields.")
        info = raw[2]
        ident, category, name = info[0], info[2], info[3]
        if (not isinstance(ident, str) or not ident or ident in seen or not isinstance(name, str)
                or type(category) is not int or category not in (0, 1, 2, 3, 4)):
            return unavailable("ambiguous team identities or unknown participation categories.")
        seen.add(ident)
        # QOJ's own UI distinguishes normal official (0), unofficial (1),
        # virtual (2), and imported official/unofficial ghosts (3/4).
        if category == 2:
            continue
        values = raw_scores.get(ident)
        if not isinstance(values, (dict, list)):
            return unavailable("a non-VP team is missing its problem results.")
        cells = {label: Cell() for label in labels}
        points = 0
        unsafe = []
        items = values.items() if isinstance(values, dict) else enumerate(values)
        for index, col in items:
            if not str(index).isdigit() or int(index) >= len(labels):
                return unavailable("unrecognized problem indexes.")
            if col is None:  # QOJ renders an absent result as an empty cell.
                continue
            if (not isinstance(col, list) or len(col) < 6
                    or not all(number(col[i]) for i in (0, 1, 4))
                    or type(col[3]) is not int or col[3] < 0 or col[5] not in (0, 1)
                    or col[4] <= 0 or col[0] not in (0, col[4])):
                return unavailable("an unsupported, partial or malformed problem result.")
            solved = col[0] == col[4]
            if solved and col[1] < 0:
                return unavailable("a solved problem has no solve time.")
            label = labels[int(index)]
            cells[label] = (Cell(pending=col[3] + 1) if col[5] else
                            Cell(col[3] + 1, 0, solved, int(col[1] // 60) if solved else None))
            points += col[0]
            if (elapsed is not None and contest.rules.frozen(elapsed, released)
                    and not col[5] and col[1] >= contest.rules.freeze_at):
                # A result explicitly dated after the local freeze is not
                # evidence of a pre-freeze score. Hide only that cell rather
                # than trusting an unmasked/final response or losing the board.
                unsafe.append(label)
        row = TeamScore(ident, name, cells, eligible=category in (0, 3),
                        penalty_minutes=contest.rules.penalty_minutes)
        if (not number(raw[0]) or not number(raw[1]) or raw[1] < 0
                or raw[0] != points or int(raw[1] // 60) != row.penalty):
            return unavailable("team totals do not match the visible problem results.")
        for label in unsafe:
            row.cells[label] = Cell(unresolved=1)
        rows.append(row)
    if not any(row.eligible for row in rows):
        return unavailable("no explicitly official non-VP teams were provided.")
    # No eligibility archive, event stream or final-board completeness is
    # inferred from this view, even after the current VP has ended.
    message = "QOJ 参考排名 · 当前公开快照，可能随 VP 时间截断；非原赛提交回放"
    if elapsed is not None and contest.rules.frozen(elapsed, released):
        message += " · 封榜：名次使用本人公开成绩，?N 不计 AC；提交/尝试统计含公开 ? 次数"
    if any(cell.unresolved for row in rows for cell in row.cells.values()):
        message += " · 越过封榜时间且未标记隐藏的单元格已局部屏蔽"
    return Reference(rows, released=released, qoj_snapshot=True, message=message)


def public_statistics(reference: Reference, problems: list[str]) -> dict:
    """Count QOJ's public cells, including publicly visible ?N attempts.

    A ?N does not disclose how many failures preceded the freeze. These are
    current public attempt counts, never claimed to be pre-freeze-only counts.
    """
    result = statistics(reference.rows, problems)
    for problem in problems:
        if any(row.eligible and row.cells[problem].unresolved for row in reference.rows):
            for key in result:
                result[key][problem] = None
    return result
