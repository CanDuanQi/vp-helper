"""Synthetic QOJ snapshots: no account, contest or network state is modified."""
from concurrent.futures import Future
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rich.console import Console

from test_regressions import OfflineTest, contest, soup
from vp_helper import cli
from vp_helper.models import Medals, Reference, Rules, Submission, VPError
from vp_helper.qoj import QOJ
from vp_helper.qoj_snapshot import reference_from_html
from vp_helper.scoring import rank_label, revealed_problems, score_submissions, statistics
from vp_helper.storage import read_history, read_json, write_json
from vp_helper.tui import Dashboard


def cell(at=60, wrong=0, *, solved=True, hidden=0):
    return [100 if solved else 0, at, -1, wrong, 100, hidden, []]


def board(rows=None, scores=None, **extra):
    if rows is None:
        rows = [[100, 60, ["official", 1500, 0, "Official team"], 1, 100],
                [100, 120, ["ghost", 1500, 3, "Imported team"], 2, 90],
                [100, 0, ["vp", 1500, 2, "Other VP"], 1, 100],
                [100, 0, ["star", 1500, 1, "Unofficial team"], 0, 100],
                [100, 0, ["star-ghost", 1500, 4, "Unofficial import"], 0, 100]]
    if scores is None:
        scores = {"official": {"0": cell(60)}, "ghost": [cell(120), None],
                  "vp": [cell(0)], "star": [cell(0)], "star-ghost": [cell(0)]}
    data = {"standings_version": 2, "standings": rows, "score": scores,
            "problems_id": ["A", "B"], "icpc_partial_scores": False}
    data.update(extra)
    return "<script>" + "\n".join(k + "=" + json.dumps(v) + ";" for k, v in data.items()) + "</script>"


class SnapshotTests(OfflineTest):
    def test_official_categories_include_imports_and_exclude_vp(self):
        ref = reference_from_html(board(), contest())
        self.assertTrue(ref.qoj_snapshot)
        self.assertTrue(ref.rank_available)
        self.assertEqual({r.id for r in ref.rows if r.eligible}, {"official", "ghost"})
        self.assertNotIn("vp", {r.id for r in ref.rows})
        self.assertEqual({r.id for r in ref.rows if not r.eligible}, {"star", "star-ghost"})
        self.assertEqual(statistics(ref.rows, ["A", "B"])["accepted"], {"A": 2, "B": 0})
        me = score_submissions([Submission("1", "me", "A", 90, "CORRECT")], ["A", "B"], Rules(18000), 100)
        self.assertEqual(rank_label(me, ref.rows), "=1")

    def test_snapshot_never_becomes_a_verified_replay_or_final(self):
        c = contest()
        c.medals = Medals(1, 2, 3, "verified archive")
        for released in (False, True):
            with self.subTest(released=released):
                ref = reference_from_html(board(medals_cutoff=[10, 20, 30]), c, released=released)
                self.assertFalse(ref.complete)
                self.assertFalse(ref.statistics_complete)
                self.assertIsNone(ref.replay)
                self.assertEqual(c.medals.gold, 1)
                self.assertIn("快照", ref.message)
                self.assertIn("非原赛", ref.message)

    def test_empty_list_and_sparse_dictionary_results(self):
        rows = [[0, 0, ["zero", 1500, 0, "Zero solves"]]]
        for values in ([], {}, [None, None], {"0": None}):
            with self.subTest(values=values):
                ref = reference_from_html(board(rows, {"zero": values}), contest())
                self.assertTrue(ref.qoj_snapshot)
                self.assertEqual(ref.rows[0].solved, 0)

    def test_attempt_counts_come_from_visible_qoj_cells(self):
        rows = [[100, 1260, ["team", 1500, 3, "Team"]]]
        scores = {"team": {"0": cell(60, 1), "1": cell(90, 2, solved=False)}}
        ref = reference_from_html(board(rows, scores), contest())
        self.assertTrue(ref.qoj_snapshot)
        self.assertEqual(ref.rows[0].penalty, 21)
        stats = statistics(ref.rows, ["A", "B"])
        self.assertEqual(stats["submitted"], {"A": 2, "B": 3})
        self.assertEqual(stats["attempted"], {"A": 1, "B": 1})

    def test_hidden_results_preserve_public_comparison(self):
        rows = [[0, 0, ["team", 1500, 3, "Team"]]]
        ref = reference_from_html(board(rows, {"team": [cell(solved=False, hidden=1)]}), contest())
        self.assertTrue(ref.qoj_snapshot)
        self.assertTrue(ref.rank_available)
        self.assertEqual(ref.rows[0].cells["A"].pending, 1)

    def test_bad_labels_partial_rules_and_unknown_versions_are_unavailable(self):
        for overrides in ({"problems_id": ["B", "A"]}, {"icpc_partial_scores": True}, {"standings_version": 99}):
            with self.subTest(overrides=overrides):
                ref = reference_from_html(board(**overrides), contest())
                self.assertFalse(ref.qoj_snapshot)
                self.assertFalse(ref.rank_available)
                self.assertEqual(ref.rows, [])

    def test_unknown_identity_duplicate_identity_and_missing_scores_are_unavailable(self):
        cases = [board([[0, 0, ["team", 0, 9, "Unknown"]]], {"team": []}),
                 board([[0, 0, ["team", 0, 0, "One"]], [0, 0, ["team", 0, 3, "Duplicate"]]], {"team": []}),
                 board([[0, 0, ["team", 0, 0, "Missing results"]]], {})]
        for html in cases:
            with self.subTest(html=html):
                self.assertFalse(reference_from_html(html, contest()).qoj_snapshot)

    def test_inconsistent_totals_and_malformed_cells_are_unavailable(self):
        for value in (cell(60), [50, 60, -1, 0, 100, 0], [100, -1, -1, 0, 100, 0],
                      [100, float("nan"), -1, 0, 100, 0], [0, 60, -1, -1, 100, 0], [0]):
            with self.subTest(value=value):
                html = board([[100, 9999, ["team", 0, 3, "Team"]]], {"team": [value]})
                self.assertFalse(reference_from_html(html, contest()).qoj_snapshot)

    def test_only_vp_or_unofficial_rows_do_not_invent_rank_one(self):
        for category in (1, 2, 4):
            with self.subTest(category=category):
                html = board([[0, 0, ["team", 0, category, "Team"]]], {"team": []})
                self.assertFalse(reference_from_html(html, contest()).rank_available)

    def test_snapshot_does_not_unlock_ccpc_problem_labels(self):
        c = contest("2026 CCPC Example Reg")
        c.rules.problem_reveal_count = 1
        ref = reference_from_html(board(), c)
        self.assertEqual(revealed_problems(c, 600, ref), frozenset())


class SourcePriorityTests(OfflineTest):
    def api(self):
        with patch("vp_helper.qoj.WebSession"):
            api = QOJ("https://qoj.ac/contest/2565", {"handle": "example"})
        api.web.soup.return_value = soup(board())
        return api

    def test_verified_original_always_wins(self):
        api = self.api()
        original = Reference(complete=True, statistics_complete=True, replay=object())
        with patch.object(api, "_original_reference", return_value=original) as strict, \
             patch("vp_helper.qoj.qoj_snapshot", side_effect=AssertionError("Must not use fallback")):
            self.assertIs(api.reference(contest(), 600), original)
        self.assertEqual(strict.call_args.kwargs, {"fetch_final": True})

    def test_unavailable_archive_falls_back_without_an_extra_qoj_request(self):
        api = self.api()
        with patch.object(api, "_original_reference", return_value=Reference(message="Archive unavailable")):
            ref = api.reference(contest(), 600)
        self.assertTrue(ref.qoj_snapshot)
        self.assertIn("Archive unavailable", ref.message)
        self.assertEqual(api.web.soup.call_count, 1)
        api.web.get.assert_not_called()
        api.web.post.assert_not_called()

    def test_archive_network_error_still_allows_qoj_snapshot(self):
        api = self.api()
        with patch.object(api, "_original_reference", side_effect=VPError("Archive network failed")):
            self.assertTrue(api.reference(contest(), 600).qoj_snapshot)

    def test_both_sources_unavailable_preserve_unknown(self):
        api = self.api()
        api.web.soup.return_value = soup("<p>No scoreboard data</p>")
        with patch.object(api, "_original_reference", return_value=Reference(message="Archive unavailable")):
            ref = api.reference(contest(), 600)
        self.assertFalse(ref.rank_available)
        self.assertIn("Archive unavailable", ref.message)
        self.assertIn("QOJ reference unavailable", ref.message)

    def test_failed_snapshot_refresh_clears_old_reference_but_not_personal_data(self):
        api = self.api()
        with patch.object(api, "_original_reference", return_value=Reference()):
            self.assertTrue(api.reference(contest(), 600).qoj_snapshot)
        api.web.soup.side_effect = VPError("QOJ HTTP 429")
        ref = api.reference(contest(), 630)
        self.assertFalse(ref.rank_available)
        self.assertEqual(ref.rows, [])
        self.assertIn("429", ref.message)
        api.web.post.assert_not_called()

    def test_verified_original_can_replace_snapshot_on_later_refresh(self):
        api = self.api()
        with patch.object(api, "_original_reference", return_value=Reference()):
            self.assertTrue(api.reference(contest(), 600).qoj_snapshot)
        original = Reference(complete=True, statistics_complete=True)
        with patch.object(api, "_original_reference", return_value=original):
            self.assertIs(api.reference(contest(), 630), original)
        self.assertFalse(api._using_qoj_snapshot)


class DashboardTests(OfflineTest):
    def test_snapshot_teams_are_scrollable_and_source_is_labelled(self):
        c = contest()
        ref = reference_from_html(board(), c)
        me = score_submissions([], list(c.problems), c.rules, 600)
        dashboard = Dashboard(c, 600, me, "3", "Unknown", statistics(ref.rows, list(c.problems)), [], reference=ref)
        out = StringIO()
        Console(file=out, width=160, color_system=None).print(dashboard)
        text = out.getvalue()
        self.assertIn("QOJ 参考排名", text)
        self.assertIn("QOJ 参考榜单", text)
        self.assertIn("非原赛回放", text)
        self.assertIn("Official team", text)
        self.assertIn("Imported team", text)
        self.assertNotIn("Other VP", text)
        self.assertIn("Unknown", text)

    def test_frozen_reference_preserves_public_scores_and_reveal_rules(self):
        c = contest("2026 CCPC Example Reg")
        c.rules = Rules(18000, freeze_at=14400, problem_reveal_count=50)
        ref = reference_from_html(board(), c)
        dashboard = Dashboard(c, 600, score_submissions([], list(c.problems), c.rules, 600),
                              "—", "Unknown", None, [], revealed=frozenset(), reference=ref)
        self.assertEqual(dashboard.public_columns(), [None, None])
        self.assertEqual([col.header for col in dashboard.reference_standings().columns][-2:], ["?", "?"])
        dashboard.elapsed = 14400
        self.assertEqual(dashboard.public_columns(), ["A", "B"])
        ranks = list(dashboard.reference_standings().columns[0]._cells)
        self.assertEqual(sorted(r for r in ranks if r != "*"), ["1", "2"])
        self.assertEqual(ranks.count("*"), 2)
        frozen = dashboard.reference_standings()
        self.assertEqual(set(frozen.columns[2]._cells), {"1"})
        self.assertEqual(set(frozen.columns[3]._cells), {"0", "1", "2"})
        self.assertTrue(all(value.startswith("+") for value in frozen.columns[4]._cells))
        ref.released = True
        self.assertEqual(set(dashboard.reference_standings().columns[2]._cells), {"1"})

    def test_monitor_snapshot_does_not_award_medals_or_write_original_rank(self):
        c = contest()
        c.medals = Medals(1, 0, 0, "original quota")
        ref = reference_from_html(board(), c, released=True)
        events = [Submission("1", "me", "A", 60, "CORRECT")]
        path = self.root / "vp-example.json"
        write_json(path, {"contest": c.to_dict(), "url": c.url, "start_time": 1000,
                         "handle": "example", "status": "running", "history_path": str(self.root / "history.csv"),
                         "submissions": [asdict(event) for event in events]})
        api = Mock(handle="example", submissions_complete=True, submission_message="")
        api.submissions.return_value = events
        api.reference.return_value = ref

        class Executor:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def submit(self, fn, *args):
                future = Future()
                future.set_result(fn(*args))
                return future

        view = Mock()
        view.input.side_effect = [True, True, False]
        with patch("vp_helper.cli.adapter", return_value=api), patch("vp_helper.cli.ThreadPoolExecutor", return_value=Executor()), \
             patch("vp_helper.cli.TerminalDashboard") as terminal, \
             patch("vp_helper.cli.time.time", return_value=19001), \
             patch("vp_helper.cli.time.monotonic", side_effect=[200, 201]), patch("vp_helper.cli.time.sleep"):
            terminal.return_value.__enter__.return_value = view
            cli.monitor(path)
        panel = view.update.call_args.args[0]
        self.assertEqual(panel.rank, "=1")
        self.assertEqual(panel.prize, "Unknown")
        self.assertEqual(panel.stats["accepted"]["A"], 2)  # Does not count this VP a second time.
        self.assertIs(panel.reference, ref)
        saved = read_json(path)
        self.assertEqual(saved["start_time"], 1000)
        self.assertEqual(saved["submissions"], [asdict(event) for event in events])
        self.assertNotIn("medal_lines", saved)
        history = read_history(self.root / "history.csv")[0]
        self.assertEqual(history["rank"], "")
        self.assertEqual(history["prize"], "Unknown")


class SubmissionLanguageTests(OfflineTest):
    def test_bundled_submit_sends_cpp23_for_cpp_and_cc_and_keeps_python(self):
        script = Path(__file__).resolve().parents[1] / "third_party/qoj/submit"
        for suffix, language in ((".cpp", "C++23"), (".cc", "C++23"), (".py", "PyPy3")):
            with self.subTest(suffix=suffix):
                source = self.root / ("A" + suffix)
                source.write_text("// synthetic source, never uploaded\n")
                config = {"name": "Synthetic contest", "base_url": "https://qoj.ac/contest/2565",
                          "problems": "AB", "offset": 101, "user_agent": "test", "cookie": "fake"}
                session = Mock()
                session.get.return_value = SimpleNamespace(text='<form id="form-answer" method="post"><input name="_token" value="fake"></form>',
                    url="https://qoj.ac/contest/2565/problem/101", raise_for_status=Mock())
                session.post.return_value = SimpleNamespace(status_code=302, headers={"Location": "/submission/123"},
                    url="https://qoj.ac/contest/2565/problem/101")
                with patch.dict(sys.modules, {"curl_cffi": None}), patch("requests.Session", return_value=session), \
                     patch("vp_helper.submit_context.submission_configuration", return_value=(self.root / "contest.json", config)), \
                     patch("vp_helper.browser_login.refresh_submission_login"), patch("vp_helper.receipts.record_receipt"), \
                     patch("sys.argv", [str(script), str(source), "-y"]), patch("sys.stdout", new=StringIO()):
                    runpy.run_path(str(script), run_name="__main__")
                self.assertEqual(session.post.call_count, 1)
                self.assertEqual(session.post.call_args.kwargs["files"]["answer_answer_language"], (None, language))
