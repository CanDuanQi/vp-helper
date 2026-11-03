"""Render regressions without a terminal, judge requests or a real VP."""
from dataclasses import replace
from unittest.mock import Mock, patch

from test_regressions import OfflineTest, contest
from vp_helper.models import Cell, Reference, Rules, TeamScore
from vp_helper.scoring import rank_label
from vp_helper.tui import Dashboard, TerminalDashboard


class DashboardPerformanceTests(OfflineTest):
    def dashboard(self):
        c = contest()
        rows = [TeamScore("one", "First", {"A": Cell(1, 0, True, 10), "B": Cell()}),
                TeamScore("two", "Tied", {"A": Cell(1, 0, True, 10), "B": Cell()}),
                TeamScore("star", "Starred", {"A": Cell(1, 0, True, 5), "B": Cell()}, eligible=False),
                TeamScore("three", "Third", {"A": Cell(1, 0, True, 20), "B": Cell(pending=2)})]
        return Dashboard(c, 600, rows[0], "=1", "Unknown", None,
                         [{"id": "1", "problem": "A", "time": 100, "verdict": "PENDING"}],
                         reference=Reference(rows=rows, qoj_snapshot=True))

    def test_bulk_ranks_match_existing_rules_for_ties_gaps_and_starred(self):
        panel = self.dashboard()
        table = panel.reference_standings()
        rows = sorted(panel.reference.rows, key=lambda row: row.sort_key)
        expected = [rank_label(row, rows) if row.eligible else "*" for row in rows]
        self.assertEqual(list(table.columns[0]._cells), expected)
        self.assertEqual(expected, ["*", "=1", "=1", "3"])
        self.assertIn("?2", list(table.columns[-1]._cells))

    def test_unknown_public_results_keep_ranks_unknown(self):
        panel = self.dashboard()
        panel.reference.rows[0].cells["B"].unresolved = 1
        self.assertEqual(list(panel.reference_standings().columns[0]._cells), ["*", "—", "—", "—"])

    def test_rank_work_grows_linearly_instead_of_scanning_board_per_team(self):
        panel = self.dashboard()
        rows = panel.reference.rows * 25
        # Use distinct IDs, as QOJ's validated adapter requires.
        panel.reference.rows = [replace(row, id=str(i)) for i, row in enumerate(rows)]
        original = TeamScore.rank_key.fget
        accesses = []

        def counted(row):
            accesses.append(row.id)
            return original(row)

        with patch.object(TeamScore, "rank_key", property(counted)):
            panel.reference_standings()
        self.assertLessEqual(len(accesses), 3 * len(rows))

    def test_clock_scroll_and_message_changes_reuse_record_layout(self):
        panel = self.dashboard()
        view = TerminalDashboard()
        with patch.object(view, "_lines", wraps=view._lines) as render:
            first = view._record_lines(panel, 124)
            for elapsed in (601, 602, 603):
                panel = replace(panel, elapsed=elapsed, message="Results stale")
                view.offset += 1
                self.assertIs(view._record_lines(panel, 124), first)
            render.assert_called_once()

    def test_resize_and_in_place_verdict_update_invalidate_layout(self):
        panel = self.dashboard()
        view = TerminalDashboard()
        with patch.object(view, "_lines", wraps=view._lines) as render:
            view._record_lines(panel, 124)
            panel.submissions[0]["verdict"] = "CORRECT"
            lines = view._record_lines(panel, 124)
            self.assertIn("CORRECT", "".join(s.text for line in lines for s in line))
            view._record_lines(panel, 100)
            self.assertEqual(render.call_count, 3)

    def test_new_snapshot_and_unfreeze_replace_cached_public_results(self):
        panel = self.dashboard()
        view = TerminalDashboard()
        before = view._record_lines(panel, 124)
        new_rows = [replace(row, cells={"A": Cell(1, 0, True, 10), "B": Cell(1, 0, True, 250)})
                    for row in panel.reference.rows]
        panel.reference = Reference(rows=new_rows, qoj_snapshot=True, released=True)
        after = view._record_lines(panel, 124)
        self.assertIsNot(before, after)
        self.assertIn("250", "".join(s.text for line in after for s in line))
        self.assertIs(view._record_lines(panel, 124), after)

    def test_reveal_boundary_rebuilds_layout_without_new_snapshot(self):
        panel = self.dashboard()
        panel.contest.rules = Rules(18000, freeze_at=14400, problem_reveal_count=50)
        panel.revealed = frozenset()
        view = TerminalDashboard()
        with patch.object(view, "_lines", wraps=view._lines) as render:
            view._record_lines(panel, 124)
            panel.elapsed = 14400
            view._record_lines(panel, 124)
            self.assertEqual(render.call_count, 2)

    def test_scroll_uses_cached_lines_and_preserves_the_full_board(self):
        panel = self.dashboard()
        panel.reference.rows = [replace(panel.reference.rows[0], id=str(i), name=f"Team {i}")
                                for i in range(30)]
        view = TerminalDashboard()
        view.screen = Mock()
        view.screen.getmaxyx.return_value = (30, 128)
        view.curses = Mock()
        view._draw = Mock()
        with patch.object(panel, "records", wraps=panel.records) as records:
            view.update(panel)
            view.offset = 10000
            view.update(panel)
            records.assert_called_once()
        self.assertGreater(view.total_lines, view.page_size)
        self.assertEqual(view.offset, view.total_lines - view.page_size)
        painted = "".join(s.text for call in view._draw.call_args_list for s in call.args[1])
        self.assertIn("Team 29", painted)

    def test_plain_contest_records_still_refresh_on_new_submission(self):
        panel = self.dashboard()
        panel.reference = None
        view = TerminalDashboard()
        view._record_lines(panel, 124)
        panel.submissions.append({"id": "2", "problem": "B", "time": 200, "verdict": "CORRECT"})
        text = "".join(s.text for line in view._record_lines(panel, 124) for s in line)
        self.assertIn("#2", text)
        self.assertIn("CORRECT", text)
