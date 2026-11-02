"""Frozen QOJ public views are recoverable without any pre-freeze local cache."""
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from test_regressions import OfflineTest, contest, soup
from test_qoj_snapshot import board, cell
from vp_helper import cli
from vp_helper.models import Rules, Submission, VPError
from vp_helper.qoj import QOJ
from vp_helper.qoj_snapshot import public_statistics, reference_from_html
from vp_helper.scoring import rank_label, score_submissions
from vp_helper.storage import read_json, write_json
from vp_helper.tui import Dashboard


def native_board(*, frozen=True, released=False, hidden_count=2):
    red_b = (cell(15120, 1) if released else
             cell(0, hidden_count - 1, solved=False, hidden=1) if frozen else cell(7200, 0, solved=False))
    rows = [[200 if released else 100, 21120 if released else 4800, ["red", 1500, 3, "Red team"]],
            [100, 4200, ["blue", 1500, 0, "Blue team"]],
            [200, 0, ["vp", 1500, 2, "Excluded VP"]]]
    return board(rows, {"red": {"0": cell(3600, 1), "1": red_b},
                        "blue": [cell(4200), cell(7200, 2, solved=False)], "vp": []})


class FrozenSnapshotTests(OfflineTest):
    def c(self):
        c = contest()
        c.rules = Rules(18000, freeze_at=14400)
        return c

    def test_before_freeze_normal_public_board(self):
        ref = reference_from_html(native_board(frozen=False), self.c(), elapsed=14399)
        self.assertTrue(ref.rank_available)
        self.assertEqual([(r.solved, r.penalty) for r in ref.rows], [(1, 80), (1, 70)])
        self.assertEqual(ref.rows[0].cells["A"].tries, 2)
        self.assertEqual(ref.rows[0].cells["A"].solve_time, 60)
        self.assertEqual(ref.rows[0].cells["B"].tries, 1)

    def test_freeze_hides_only_pending_cells_and_preserves_public_totals(self):
        c = self.c()
        ref = reference_from_html(native_board(), c, elapsed=15000)
        self.assertTrue(ref.qoj_snapshot)
        self.assertTrue(ref.rank_available)
        red, blue = ref.rows
        self.assertEqual((red.solved, red.penalty, rank_label(red, ref.rows)), (1, 80, "2"))
        self.assertEqual((blue.solved, blue.penalty, rank_label(blue, ref.rows)), (1, 70, "1"))
        self.assertTrue(red.cells["A"].solved)
        self.assertEqual((red.cells["A"].tries, red.cells["A"].solve_time), (2, 60))
        self.assertFalse(red.cells["B"].solved)
        self.assertEqual(red.cells["B"].pending, 2)
        self.assertEqual(public_statistics(ref, ["A", "B"]), {
            "submitted": {"A": 3, "B": 5}, "attempted": {"A": 2, "B": 2}, "accepted": {"A": 2, "B": 0}})

    def test_question_mark_counts_follow_current_qoj_markup(self):
        for count in (1, 2, 11):
            with self.subTest(count=count):
                c = self.c()
                ref = reference_from_html(native_board(hidden_count=count), c, elapsed=15000)
                dashboard = Dashboard(c, 15000, score_submissions([], ["A", "B"], c.rules, 15000),
                                      "3", "Unknown", public_statistics(ref, ["A", "B"]), [], reference=ref)
                table = dashboard.reference_standings()
                self.assertIn("?" + str(count), table.columns[5]._cells)
                self.assertIn("+1\n60", table.columns[4]._cells)
                self.assertNotIn("—", table.columns[2]._cells)

    def test_first_launch_after_freeze_uses_current_page_without_cache(self):
        c = self.c()
        with patch("vp_helper.qoj.WebSession"):
            api = QOJ(c.url, {"handle": "example"})
        api.web.soup.return_value = soup(native_board())
        with patch.object(api, "_original_reference", side_effect=VPError("No XCPCIO/SRK archive")), \
             patch("vp_helper.storage.write_json", side_effect=AssertionError("No cache is needed")):
            ref = api.reference(c, 15000)
        self.assertTrue(ref.rank_available)
        self.assertEqual(ref.rows[0].solved, 1)
        self.assertEqual(ref.rows[0].cells["B"].pending, 2)
        api.web.post.assert_not_called()
        api.start.assert_not_called()

    def test_restart_before_and_after_freeze_matches_fresh_public_page(self):
        c = self.c()
        before = reference_from_html(native_board(frozen=False), c, elapsed=14399)
        first_frozen = reference_from_html(native_board(), c, elapsed=15000)
        fresh_after_restart = reference_from_html(native_board(), self.c(), elapsed=15100)
        self.assertEqual([asdict(r) for r in first_frozen.rows], [asdict(r) for r in fresh_after_restart.rows])
        self.assertEqual([(r.solved, r.penalty) for r in before.rows],
                         [(r.solved, r.penalty) for r in fresh_after_restart.rows])
        self.assertEqual(public_statistics(before, ["A", "B"])["accepted"],
                         public_statistics(fresh_after_restart, ["A", "B"])["accepted"])

    def test_native_unfreeze_restores_ac_penalty_rank_and_statistics(self):
        c = self.c()
        frozen = reference_from_html(native_board(), c, elapsed=17999)
        released = reference_from_html(native_board(released=True), c, elapsed=18001, released=True)
        self.assertEqual(frozen.rows[0].solved, 1)
        self.assertEqual(released.rows[0].solved, 2)
        self.assertEqual(released.rows[0].penalty, 352)
        self.assertEqual(rank_label(released.rows[0], released.rows), "1")
        self.assertEqual(public_statistics(released, ["A", "B"])["accepted"]["B"], 1)
        self.assertFalse(released.complete)
        self.assertFalse(released.statistics_complete)
        self.assertIsNone(released.replay)

    def test_unmasked_post_freeze_result_does_not_hide_the_other_public_cells(self):
        c = self.c()
        ref = reference_from_html(native_board(released=True), c, elapsed=15000)
        self.assertTrue(ref.qoj_snapshot)
        self.assertFalse(ref.rank_available)
        red, blue = ref.rows
        self.assertTrue(red.cells["A"].solved)
        self.assertTrue(red.cells["B"].unresolved)
        self.assertFalse(red.cells["B"].solved)
        self.assertTrue(blue.results_complete)
        stats = public_statistics(ref, ["A", "B"])
        self.assertEqual(stats["accepted"]["A"], 2)
        self.assertIsNone(stats["accepted"]["B"])
        dashboard = Dashboard(c, 15000, score_submissions([], ["A", "B"], c.rules, 15000),
                              "—", "Unknown", stats, [], reference=ref)
        self.assertEqual(dashboard.statistic("accepted", "B"), "—")
        table = dashboard.reference_standings()
        self.assertIn("+1\n60", table.columns[4]._cells)
        self.assertIn("?", table.columns[5]._cells)
        self.assertIn("1", table.columns[2]._cells)
        self.assertIn("—", table.columns[2]._cells)

    def test_frozen_reference_name_and_prizes_remain_distinct_from_original_replay(self):
        ref = reference_from_html(native_board(), self.c(), elapsed=15000)
        self.assertFalse(ref.complete)
        self.assertFalse(ref.statistics_complete)
        self.assertIsNone(ref.replay)
        self.assertIn("公开", ref.message)
        self.assertIn("非原赛", ref.message)
        self.assertIn("统计含公开", ref.message)

    def test_monitor_restart_keeps_private_ac_but_ranks_only_the_public_score(self):
        c = self.c()
        events = [Submission("1", "me", "A", 6000, "CORRECT"),
                  Submission("2", "me", "B", 15000, "CORRECT")]
        path = self.root / "vp-frozen.json"
        write_json(path, {"contest": c.to_dict(), "url": c.url, "start_time": 1000,
                         "handle": "example", "status": "running", "history_path": str(self.root / "history.csv"),
                         "submissions": [asdict(e) for e in events]})

        class Executor:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def submit(self, fn, *args):
                future = Future()
                future.set_result(fn(*args))
                return future

        for now, released in ((16100, False), (16200, False), (19001, True)):
            with self.subTest(now=now):
                ref = reference_from_html(native_board(released=released), c, elapsed=now - 1000, released=released)
                api = Mock(handle="example", submissions_complete=True, submission_message="")
                api.submissions.return_value = events
                api.reference.return_value = ref
                view = Mock()
                view.input.side_effect = [True, True, False]
                with patch("vp_helper.cli.adapter", return_value=api), \
                     patch("vp_helper.cli.ThreadPoolExecutor", return_value=Executor()), \
                     patch("vp_helper.cli.TerminalDashboard") as terminal, \
                     patch("vp_helper.cli.time.time", return_value=now), \
                     patch("vp_helper.cli.time.monotonic", side_effect=[200, 201]), patch("vp_helper.cli.time.sleep"):
                    terminal.return_value.__enter__.return_value = view
                    cli.monitor(path)
                panel = view.update.call_args.args[0]
                self.assertEqual(panel.me.solved, 2)
                self.assertEqual(panel.rank, "1" if released else "3")
                self.assertEqual(panel.prize, "Unknown")
                self.assertEqual(panel.stats["accepted"]["B"], 1 if released else 0)
                self.assertEqual(read_json(path)["start_time"], 1000)
                self.assertEqual(read_json(path)["submissions"], [asdict(e) for e in events])
                api.start.assert_not_called()
