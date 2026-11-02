"""QOJ's visible scoreboard, kept separate from verified original replays."""
from __future__ import annotations

import math

from .models import Cell, Contest, Reference, TeamScore
from .parsing import js_value


def reference_from_html(html: str, contest: Contest, *, released: bool = False) -> Reference:
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
        row = TeamScore(ident, name, cells, eligible=category in (0, 3),
                        penalty_minutes=contest.rules.penalty_minutes)
        if (not number(raw[0]) or not number(raw[1]) or raw[1] < 0
                or raw[0] != points or int(raw[1] // 60) != row.penalty):
            return unavailable("team totals do not match the visible problem results.")
        rows.append(row)
    if not any(row.eligible for row in rows):
        return unavailable("no explicitly official non-VP teams were provided.")
    # No eligibility archive, event stream or final-board completeness is
    # inferred from this view, even after the current VP has ended.
    return Reference(rows, released=released, qoj_snapshot=True,
                     message="QOJ 参考排名 · 当前原站快照，可能随 VP 时间截断或封榜；非原赛提交回放")
