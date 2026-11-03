"""Exercise the installed-style submit script without any real upload."""
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import runpy
import sys
from unittest.mock import Mock, patch

from test_regressions import OfflineTest
from vp_helper.receipts import qoj_submission_error, qoj_submission_id


URL = "https://qoj.ac/contest/4616/problem/23656"
LANG_URL = URL + "/statement/zh_cn"
LIST_URL = "https://qoj.ac/contest/4616/submissions"


def response(text="", *, url=LANG_URL, status=200, location=None):
    return SimpleNamespace(text=text, url=url, status_code=status,
                           headers={"Location": location} if location else {}, raise_for_status=Mock())


def form(action=LANG_URL, token="answer-token"):
    return f'''<form id="other"><input name="_token" value="wrong-token"></form>
        <form id="form-answer" method="post" action="{action}">
        <input name="_token" value="{token}"><button name="submit-answer" value="answer"></button></form>'''


def listing(*ids):
    return "<table>" + "".join(f'''<tr><td><a href="/submission/{sid}">#{sid}</a></td>
        <td><a href="/contest/4616/problem/23656">A</a></td>
        <td><span class="uoj-username">example</span></td></tr>''' for sid in ids) + "</table>"


class SubmitConfirmationTests(OfflineTest):
    def run_submit(self, *, page=None, post=None, lists=None, curl=False):
        source = self.root / "A.cpp"
        source.write_text("int main(){}\n")
        config = {"name": "Synthetic contest", "base_url": "https://qoj.ac/contest/4616", "problems": "A",
                  "offset": 23656, "handle": "example", "cookie": "fake-cookie", "user_agent": "test"}
        session = Mock()
        session.get.side_effect = [page or response(form())] + (lists or [response(listing("10"), url=LIST_URL)])
        if isinstance(post, Exception):
            session.post.side_effect = post
        else:
            session.post.return_value = post or response(status=302, location="/submission/11")
        script = Path(__file__).resolve().parents[1] / "third_party/qoj/submit"
        with ExitStack() as stack:
            if not curl:
                stack.enter_context(patch.dict(sys.modules, {"curl_cffi": None}))
            stack.enter_context(patch("curl_cffi.requests.Session" if curl else "requests.Session", return_value=session))
            stack.enter_context(patch("vp_helper.submit_context.submission_configuration", return_value=(self.root / "contest.json", config)))
            stack.enter_context(patch("vp_helper.browser_login.refresh_submission_login"))
            receipt = stack.enter_context(patch("vp_helper.receipts.record_receipt"))
            stack.enter_context(patch("sys.argv", [str(script), str(source), "-y"]))
            stack.enter_context(patch("sys.stdout", new=StringIO()))
            sleep = stack.enter_context(patch("time.sleep"))
            error = None
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit as exc:
                error = str(exc)
        return session, receipt, sleep, error

    def test_language_redirect_posts_to_actual_form_action_once(self):
        session, receipt, _, error = self.run_submit()
        self.assertIsNone(error)
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.args[0], LANG_URL)
        fields = session.post.call_args.kwargs["files"]
        self.assertEqual(fields["_token"], (None, "answer-token"))
        self.assertEqual(fields["answer_answer_language"], (None, "C++23"))
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        session.headers.update.assert_any_call({"Referer": LANG_URL})
        self.assertEqual(receipt.call_args.args[1], "11")

    def test_real_curl_mime_branch_uses_same_final_form_target(self):
        session, receipt, _, error = self.run_submit(curl=True)
        self.assertIsNone(error)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.post.call_args.args[0], LANG_URL)
        self.assertIn("multipart", session.post.call_args.kwargs)
        receipt.assert_called_once()

    def test_empty_action_uses_final_get_url(self):
        session, _, _, error = self.run_submit(page=response(form(action="")))
        self.assertIsNone(error)
        self.assertEqual(session.post.call_args.args[0], LANG_URL)

    def test_original_problem_form_remains_supported(self):
        session, _, _, error = self.run_submit(page=response(form(action=URL), url=URL))
        self.assertIsNone(error)
        self.assertEqual(session.post.call_args.args[0], URL)

    def test_missing_form_token_or_wrong_destination_never_posts(self):
        for html in ("<p>No form</p>", form(token=""), form(action="https://other.invalid/submit"),
                     form(action="https://qoj.ac/contest/999/problem/23656")):
            with self.subTest(html=html):
                session, receipt, _, error = self.run_submit(page=response(html))
                self.assertIn("no code was sent", error)
                session.post.assert_not_called()
                receipt.assert_not_called()

    def test_submission_list_delay_retries_only_get(self):
        session, receipt, sleep, error = self.run_submit(post=response(status=303, location=LIST_URL), lists=[
            response(listing("10"), url=LIST_URL), response(listing("10"), url=LIST_URL),
            response(listing("11", "10"), url=LIST_URL)])
        self.assertIsNone(error)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.get.call_count, 4)
        sleep.assert_called_once_with(1)
        self.assertEqual(receipt.call_args.args[1], "11")

    def test_http_200_success_without_redirect_is_verified_against_new_record(self):
        session, receipt, _, error = self.run_submit(post=response("<main>Submitted</main>"), lists=[
            response(listing("10"), url=LIST_URL), response(listing("11", "10"), url=LIST_URL)])
        self.assertIsNone(error)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(receipt.call_args.args[1], "11")

    def test_explicit_form_rejection_is_reported_and_secret_removed(self):
        session, receipt, _, error = self.run_submit(post=response('<main>This page has expired. answer-token fake-cookie</main>'))
        self.assertIn("QOJ rejected", error)
        self.assertNotIn("answer-token", error)
        self.assertNotIn("fake-cookie", error)
        self.assertEqual(session.post.call_count, 1)
        receipt.assert_not_called()

    def test_old_or_ambiguous_records_never_become_success(self):
        for html in (listing("10"), listing("12", "11", "10")):
            with self.subTest(html=html):
                session, receipt, _, error = self.run_submit(post=response(status=302, location=LIST_URL),
                    lists=[response(listing("10"), url=LIST_URL)] + [response(html, url=LIST_URL) for _ in range(3)])
                self.assertIn("not confirmed", error)
                self.assertIn("No automatic retry", error)
                self.assertEqual(session.post.call_count, 1)
                receipt.assert_not_called()

    def test_post_redirect_never_replays_upload(self):
        session, receipt, _, error = self.run_submit(post=response(status=307, location=LANG_URL))
        self.assertIn("HTTP 307", error)
        self.assertEqual(session.post.call_count, 1)
        receipt.assert_not_called()

    def test_post_timeout_never_replays_upload_or_writes_receipt(self):
        from requests import Timeout
        session, receipt, _, error = self.run_submit(post=Timeout("synthetic timeout"))
        self.assertIn("confirmation unavailable", error)
        self.assertEqual(session.post.call_count, 1)
        receipt.assert_not_called()

    def test_http_503_is_uncertain_not_a_definite_rejection(self):
        session, receipt, _, error = self.run_submit(post=response("<title>Error</title><main>Gateway failed</main>", status=503))
        self.assertIn("not confirmed", error)
        self.assertIn("503", error)
        self.assertEqual(session.post.call_count, 1)
        receipt.assert_not_called()

    def test_relative_submission_location_uses_response_url(self):
        r = response(status=302, location="/submission/11")
        self.assertEqual(qoj_submission_id(r, "https://qoj.ac"), "11")
        r.url = "https://qoj.ac/contest/4616/submissions"
        r.headers = {"Location": "../../submission/12"}
        self.assertEqual(qoj_submission_id(r, "https://qoj.ac"), "12")

    def test_error_redaction_precedes_message_truncation(self):
        secret = "secret" * 20
        r = response('<div class="alert-danger">' + "x" * 490 + secret + "</div>")
        error = qoj_submission_error(r, secrets=(secret,))
        self.assertNotIn("secret", error)
        self.assertLessEqual(len(error), 500)
