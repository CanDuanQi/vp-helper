from __future__ import annotations

from dataclasses import replace
from collections import Counter
from datetime import datetime
import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from .http import WebSession, form_values
from .models import Cell, Contest, Reference, Rules, Submission, TeamScore, VPError
from .originals import OriginalReplay, cache_archive_submissions, classify, eligibility_lookup, identity, load_archive_submissions, load_metadata, lookup_team
from .parsing import ccpc_reveal_count, directory_name, js_value, medals_from_html, seconds, timestamp
from .scoring import verdict
from .storage import read_json, state_directory, write_json


class QOJ:
    def __init__(self, url: str, account: dict):
        self.url = url
        self.id = urlsplit(url).path.rsplit("/", 1)[-1]
        self.origin = f"https://{urlsplit(url).hostname}"
        self.account = account
        self.web = WebSession(self.origin, account)
        self.handle = account.get("handle", "")
        self.start_time: float | None = None
        self.submissions_complete = False
        self._finished_start: float | None = None
        self._relative_start_verified: float | None = None
        self._verdict_cache = {}
        self._detail_retry = {}
        self.submission_message = ""
        self._replay = None
        self._replay_error = ""
        self._next_replay_attempt = 0.0
        self._public_web = None

    def _page(self, suffix: str = "", **params) -> BeautifulSoup:
        return self.web.soup(self.url + suffix, params={"locale": "en", **params})

    def prepare(self) -> Contest:
        soup = self._page()
        headings = soup.select(".uoj-content h1") or soup.select("h1")
        titles = [h.get_text(" ", strip=True) for h in headings]
        titles.extend(a.get_text(" ", strip=True) for a in soup.select('a[href*="?v="]'))
        text = soup.get_text(" ", strip=True)
        if "Ruleset: ICPC" not in text:
            raise VPError("This QOJ contest does not use ICPC scoring.")
        name = directory_name(titles, fallback=f"QOJ {self.id}")
        problem_map = {}
        for row in soup.select("tr"):
            a = row.select_one(f'a[href*="/contest/{self.id}/problem/"]')
            cells = row.select("td")
            if a and cells:
                label = cells[0].get_text(strip=True)
                if re.fullmatch(r"[A-Z][0-9]*", label):
                    problem_map[label] = urlsplit(a["href"]).path.rsplit("/", 1)[-1]
        if not problem_map:
            raise VPError("QOJ did not return the contest problem list.")
        freeze = re.search(r"Scoreboard will be frozen after\s*(\d+:\d{2}:\d{2})", text)
        penalty = re.search(r"Penalty:\s*(\d+)\s*minute", text)
        # Registrants can choose a shorter rated-VP window. It is not the
        # contest duration (4582 already has two-hour rated participations).
        duration = None
        listing = self.web.soup(self.origin + "/contests", params={"locale": "en"})
        for row in listing.select("tr"):
            if not any(urlsplit(a.get("href", "")).path == f"/contest/{self.id}" for a in row.select("a")):
                continue
            raw = row.get_text(" ", strip=True)
            match = re.search(r"([\d.]+)\s*hours?", raw)
            if match:
                duration = int(float(match[1]) * 3600)
            match = re.search(r"(\d+)\s*minutes?", raw)
            if match and not duration:
                duration = int(match[1]) * 60
        if not duration:
            duration = self._registry_duration(self._page("/registrants"))
        if not duration:
            raise VPError("QOJ did not provide the contest duration; a five-hour duration will not be guessed.")
        pdfs = {}
        for a in soup.select('a[href*="download.php"]'):
            label = a.get_text(" ", strip=True)
            if re.search(r"Statements?\b|题面", label, re.I):
                suffix = "zh-cn" if "zh" in label else "en"
                pdfs[f"Statements-{suffix}.pdf"] = urljoin(self.url, a["href"])
        if not pdfs:
            # A small number of contests use individual problem attachments.
            for label, pid in problem_map.items():
                p = self._page(f"/problem/{pid}")
                link = p.select_one('a[href$=".pdf"], iframe[src*=".pdf"], object[data*=".pdf"], '
                                    'iframe[src*="download.php"][src*="type=statement"]')
                if link:
                    pdfs[f"{label}.pdf"] = urljoin(self.url, link.get("href") or link.get("src") or link.get("data"))
        if not pdfs:
            raise VPError("QOJ did not expose a statement PDF for this contest.")
        return Contest("qoj", self.id, self.url, name, name, problem_map,
                       Rules(duration, int(penalty[1]) if penalty else 20,
                             freeze_at=seconds(freeze[1]) if freeze else None,
                             problem_reveal_count=ccpc_reveal_count(name)), pdfs)

    @staticmethod
    def _registry_duration(soup) -> int | None:
        for row in soup.select("tr"):
            cells = row.select("td")
            if len(cells) >= 5 and cells[-1].get_text(strip=True).isdigit():
                return int(cells[-1].get_text(strip=True)) * 60
        return None

    def _registration(self, first=None, duration: int | None = None) -> tuple[float, int] | None:
        soup = first or self._page("/registrants", show_unofficial="true")
        last = max([1] + [int(m[1]) for a in soup.select("a[href]")
                          if (m := re.search(r"[?&]page=(\d+)", a["href"]))])
        records, virtual = {}, set()
        recognized = has_virtual_table = False
        for page in [1] + list(range(last, 1, -1)):
            if page != 1:
                soup = self._page("/registrants", show_unofficial="true", page=page)
            for table in soup.select("table"):
                headers = [h.get_text(" ", strip=True).lower() for h in table.select("tr th")]
                user_col = next((i for i, h in enumerate(headers) if h in ("username", "user", "用户名")), None)
                start_col = next((i for i, h in enumerate(headers) if h in ("start time", "start", "开始时间")), None)
                length_col = next((i for i, h in enumerate(headers) if h in ("duration", "length", "时长")), None)
                if user_col is None:
                    continue
                if start_col is None:
                    raise VPError("QOJ's registration table start-time column could not be identified.")
                is_virtual = length_col is None and len(headers) == 3
                if length_col is None and not is_virtual:
                    raise VPError("QOJ's registration table duration column could not be identified.")
                recognized = True
                has_virtual_table |= is_virtual
                for row in table.select("tr"):
                    cells = row.find_all("td", recursive=False)
                    if len(cells) <= user_col:
                        continue
                    user = cells[user_col].select_one('a[href*="/user/profile/"], .uoj-username')
                    user_id = (urlsplit(user["href"]).path.rsplit("/", 1)[-1] if user and user.get("href")
                               else user.get_text(strip=True) if user else cells[user_col].get_text(strip=True))
                    if user_id != self.handle:
                        continue
                    if len(cells) <= max(start_col, length_col or 0):
                        raise VPError("QOJ's account registration row is missing its start time or duration.")
                    date_cell = cells[start_col]
                    node = date_cell.select_one("time[datetime]")
                    value = timestamp(node["datetime"] if node else date_cell.get_text(" ", strip=True))
                    if value <= timestamp("2000-01-01 00:00:00"):
                        raise VPError("QOJ returned an invalid account registration start time.")
                    if is_virtual:
                        virtual.add(value)
                    else:
                        minutes = re.fullmatch(r"(\d+)\s*(?:minutes?|mins?|分钟)?", cells[length_col].get_text(" ", strip=True), re.I)
                        if not minutes or int(minutes[1]) <= 0:
                            raise VPError("QOJ's account registration duration could not be parsed.")
                        records[value] = int(minutes[1]) * 60
        if not recognized:
            raise VPError("QOJ's registration table columns could not be identified; no start request was inferred.")
        starts = virtual if has_virtual_table else records.keys()
        if not starts:
            return None
        latest = max(starts)
        length = records.get(latest) or duration or self._registry_duration(soup)
        if not length:
            raise VPError("QOJ did not provide this virtual participation's duration.")
        return latest, length

    def _start_receipt(self):
        key = hashlib.sha256((self.url + "\0" + self.handle).encode()).hexdigest()[:24]
        return state_directory() / ("qoj-start-" + key + ".json")

    def _accept_start(self, contest: Contest, record: tuple[float, int]) -> float:
        self.start_time, contest.rules.duration = record
        self._start_receipt().unlink(missing_ok=True)
        return self.start_time

    def _confirm_start(self, contest: Contest, pending: dict) -> float:
        registration_error = None
        # Only GETs are retried. Allow a short propagation delay after the one
        # final registration POST, including when its response was lost.
        for delay in (0, 2, 3, 5, 8, 12):
            if delay:
                time.sleep(delay)
            try:
                record = self._registration(duration=contest.rules.duration)
                registration_error = None
            except VPError as exc:
                registration_error = exc
                continue
            if record and (pending["previous_start"] is None or record[0] > pending["previous_start"]):
                return self._accept_start(contest, record)
        reason = (f"Registration query failed: {registration_error}" if registration_error else
                  "No new account registration record was visible after 30 seconds of read-only checks")
        if pending.get("request_error"):
            reason = pending["request_error"] + ". " + reason
        raise VPError(f"QOJ VP start remains unconfirmed. {reason}. "
                      f"Run vp qoj {self.id} again to check the pending request; no second start POST will be sent. "
                      f"Pending state: {self._start_receipt()}")

    def _check_start_account(self, soup) -> None:
        nav = soup.select_one("ul.nav-pills")
        if not self.handle or not nav or not any(urlsplit(a.get("href", "")).path == f"/user/profile/{self.handle}" for a in nav.select("a")):
            raise VPError("The QOJ login cookie does not belong to the configured handle.")

    def start(self, contest: Contest) -> float:
        soup = self._page()
        self._check_start_account(soup)
        pending = read_json(self._start_receipt())
        if pending:
            # A previous process may have lost its response or exited after
            # sending the POST. Reconcile with QOJ before considering any form.
            return self._confirm_start(contest, pending)
        current = self._registration(duration=contest.rules.duration)
        if current and time.time() < current[0] + current[1]:
            return self._accept_start(contest, current)
        form = soup.select_one("form#form-regvir")
        if form is None:
            if current:
                return self._accept_start(contest, current)
            raise VPError("QOJ did not offer native virtual participation for this account.")
        # The dashboard's regvir POST only opens the rules page on current
        # QOJ. GET that page directly; the sole POST must submit register_vp.
        form_url = self.url
        try:
            response = self.web.get(self.url + "/virtual/register", params={"locale": "en"})
        except VPError as exc:
            if str(exc) != f"{urlsplit(self.origin).hostname} returned HTTP 404":
                raise
            # Older deployments start directly from the dashboard form.
        else:
            form_url = response.url
            target = urlsplit(form_url)
            if (target.scheme, target.netloc) != (urlsplit(self.origin).scheme, urlsplit(self.origin).netloc):
                raise VPError("QOJ returned an unexpected VP registration destination.")
            soup = BeautifulSoup(response.content, "html.parser")
            self._check_start_account(soup)
            if target.path == f"/contest/{self.id}/virtual/register":
                form = soup.select_one("form#form-register_vp")
            elif target.path == f"/contest/{self.id}":
                form = soup.select_one("form#form-regvir")
            else:
                form = None
            if form is None:
                raise VPError("QOJ did not provide its final native VP registration form; no start POST was sent.")
        button_name = "submit-register_vp" if form.get("id") == "form-register_vp" else "submit-regvir"
        button = form.select_one(f'button[type="submit"][name="{button_name}"], input[type="submit"][name="{button_name}"]')
        if form.get("method", "").lower() != "post" or button is None or button.has_attr("disabled"):
            raise VPError("QOJ did not provide an enabled native VP registration button.")
        payload = form_values(form)
        payload[button["name"]] = button.get("value", "")
        if not payload.get("_token"):
            raise VPError("QOJ did not provide a native VP start token.")
        action = urljoin(form_url, form.get("action") or form_url)
        target = urlsplit(action)
        if ((target.scheme, target.netloc) != (urlsplit(self.origin).scheme, urlsplit(self.origin).netloc)
                or target.path not in (f"/contest/{self.id}", f"/contest/{self.id}/virtual/register")):
            raise VPError("QOJ returned an unexpected native VP start destination.")
        pending = {"url": self.url, "handle": self.handle, "previous_start": current[0] if current else None}
        # Save before sending: a crash or timeout must never permit a second
        # POST on the next invocation. No token or cookie is persisted here.
        write_json(self._start_receipt(), pending)
        try:
            response = self.web.post(action, payload, referer=form_url)
        except VPError as exc:
            pending["request_error"] = str(exc)
            write_json(self._start_receipt(), pending)
        else:
            site_message = self._start_message(response)
            if site_message:
                for secret in (payload["_token"], self.account.get("cookie", "")):
                    if secret:
                        site_message = site_message.replace(secret, "[redacted]")
                # A definite form rejection did not create a participation.
                self._start_receipt().unlink(missing_ok=True)
                raise VPError(f"QOJ rejected the VP registration: {site_message}")
        return self._confirm_start(contest, pending)

    @staticmethod
    def _start_message(response) -> str:
        # QOJ can return a form rejection as an HTTP 200 message page.
        # Keep that explanation instead of replacing it with a generic error.
        content = getattr(response, "content", None)
        if not isinstance(content, (str, bytes)):
            return ""
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and (data.get("success") is False or data.get("error") or data.get("errors")):
            return str(data.get("error") or data.get("errors") or data.get("message") or "Registration rejected")[:500]
        soup = BeautifulSoup(content, "html.parser")
        for node in soup.select("script, style, .uoj-article"):
            node.decompose()
        errors = soup.select(".alert-danger, .has-error .help-block, .uoj-error")
        if errors:
            return " ".join(node.get_text(" ", strip=True) for node in errors)[:500]
        for node in soup.select("nav, header, footer, form, input, table"):
            node.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        main = soup.select_one(".uoj-content, main, #content")
        text = (main or soup).get_text(" ", strip=True)
        if (re.match(r"^(?:Error|错误|出错)(?:\b|\s|[-:：])", title, re.I)
                or re.search(r"this page has expired|the form is incomplete|invalid csrf token", text, re.I)
                or re.match(r"^(?:Message|Notice|消息|提示)(?:\b|\s|[-:：])", title, re.I)
                and re.search(r"denied|not allowed|cannot|failed|expired|拒绝|失败|过期", text, re.I)):
            return text[:500] or title[:500]
        # A neutral notice or an ordinary contest page is not evidence of
        # failure (or success). Only the account's native record confirms time.
        return ""

    def submit_configuration(self, contest: Contest) -> dict:
        ids = [int(x) for x in contest.problems.values()]
        return {"name": contest.name, "base_url": self.url, "problems": "".join(contest.problems),
                "handle": self.handle,
                "offset": ids[0], "user_agent": self.web.session.headers["User-Agent"],
                "cookie": self.account.get("cookie", ""), "problem_ids": contest.problems}

    def submissions(self, contest: Contest) -> list[Submission]:
        if self.start_time is None:
            raise VPError("The native QOJ VP start has not been verified.")
        result = {}
        unresolved = []
        inverse = {str(pid): label for label, pid in contest.problems.items()}
        page = 1
        while True:
            soup = self._page("/submissions", submitter=self.handle, page=page)
            found = 0
            before = False
            for row in soup.select("tr"):
                cells = row.select("td")
                id_link = row.select_one('a[href^="/submission/"]')
                user = row.select_one('.uoj-username, a[href*="/user/profile/"]')
                problem = row.select_one(f'a[href*="/contest/{self.id}/problem/"]')
                if not id_link or not user or not problem:
                    continue
                user_id = urlsplit(user["href"]).path.rsplit("/", 1)[-1] if user.get("href") else user.get_text(strip=True)
                if user_id != self.handle:
                    continue
                found += 1
                date_node = row.select_one("time[datetime]")
                dates = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row.get_text(" ", strip=True))
                if date_node or dates:
                    at = timestamp(date_node["datetime"] if date_node else dates[0]) - self.start_time
                elif cells and re.fullmatch(r"\d+:\d{2}:\d{2}", cells[-1].get_text(strip=True)) and any(
                        urlsplit(a.get("href", "")).path == f"/contest/{self.id}" for a in row.select("sup a[href]")):
                    # QOJ emits relative time only for submissions explicitly
                    # attached to this native contest, marked by the # link.
                    if self._relative_start_verified != self.start_time:
                        current = self._registration(duration=contest.rules.duration)
                        if not current or current[0] != self.start_time:
                            raise VPError("The current native QOJ VP start no longer matches this session.")
                        self._relative_start_verified = self.start_time
                    at = seconds(cells[-1].get_text(strip=True))
                else:
                    raise VPError("QOJ did not provide the submission timestamp.")
                if at < 0:
                    before = True
                    continue
                pid = urlsplit(problem["href"]).path.rsplit("/", 1)[-1]
                label = inverse.get(pid)
                if not label:
                    continue
                sid = urlsplit(id_link["href"]).path.rsplit("/", 1)[-1]
                # Score/status follows submitter and problem; infer column from its header.
                table = row.find_parent("table")
                headers = [h.get_text(" ", strip=True).lower() for h in table.select("thead th")]
                if not headers:
                    headers = [h.get_text(" ", strip=True).lower() for h in table.select("tr th")]
                column = next((i for i, h in enumerate(headers) if h in ("result", "score", "status")), 3)
                node = cells[column] if column < len(cells) else None
                if node is None:
                    raise VPError("QOJ submission result column is missing.")
                status = node.get_text(" ", strip=True)
                judging = bool(row.select_one('[id^="status_details_"]') or
                               re.search(r"update_judgement_status_details\s*\(", str(row)))
                if judging:
                    result[sid] = Submission(sid, "me", label, at, "PENDING")
                    self._verdict_cache.pop(sid, None)
                    self._detail_retry.pop(sid, None)
                    continue
                score = float(status) if re.fullmatch(r"\d+(?:\.\d+)?", status) else None
                if score is not None and score < 100:
                    # QOJ numeric zero alone does not distinguish WA/RE/MLE/CE.
                    cached = self._verdict_cache.get(sid)
                    if cached and cached[0] in (None, score) and time.monotonic() < cached[2]:
                        status = cached[1]
                    else:
                        now = time.monotonic()
                        retry_at, delay = self._detail_retry.get(sid, (0.0, 0.0))
                        if now < retry_at:
                            parsed = "UNAVAILABLE"
                        else:
                            try:
                                detail = self.web.soup(self.origin + f"/submission/{sid}",
                                                       params={"locale": "en"}, retry=False)
                                parsed = self.detail_verdict(detail, score)
                            except VPError:
                                # A zero score is final, but can mean WA, CE or
                                # another failure. Do not invent a scored WA or
                                # keep an obsolete submission receipt as pending.
                                parsed = "UNAVAILABLE"
                                delay = min(120.0, max(15.0, delay * 2))
                                self._detail_retry[sid] = (time.monotonic() + delay, delay)
                            else:
                                self._detail_retry.pop(sid, None)
                                if parsed != "PENDING":
                                    self._verdict_cache[sid] = (score, parsed, time.monotonic() + 300 + int(sid) % 300)
                        if parsed == "UNAVAILABLE":
                            unresolved.append(sid)
                            result[sid] = Submission(sid, "me", label, at, parsed)
                            continue
                        status = parsed
                result[sid] = Submission(sid, "me", label, at, verdict(status, score))
            pages = [int(m[1]) for a in soup.select("a[href]") if (m := re.search(r"[?&]page=(\d+)", a["href"]))]
            if before or not found or page >= max(pages, default=page):
                self.submissions_complete = True
                break
            page += 1
        self.submission_message = ("Result unavailable for " + ", ".join("#" + sid for sid in unresolved)
                                   + " · retrying details; other submissions are synced" if unresolved else "")
        return list(result.values())

    @staticmethod
    def detail_verdict(detail: BeautifulSoup, score: float) -> str:
        # Current QOJ nests each test's verdict inside a Bootstrap column. A
        # card header begins with "Test #N: score: ...", which is not a verdict.
        # Limit parsing to the latest Details card, excluding judging history.
        headers = [node for node in detail.select(".card-header, .panel-heading")
                   if node.get_text(" ", strip=True).lower() == "details"]
        scope = headers[-1].parent if headers else detail
        pending = False
        for node in scope.select(".uoj-status, .col-sm-2, .panel-title, .card-header, table td"):
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            try:
                parsed = verdict(text)
            except VPError:
                continue
            if parsed == "PENDING":
                pending = True
            elif parsed not in ("CORRECT", "TOO-LATE"):
                return parsed
            # Passing an earlier test must never override the final zero score.
        if pending:
            return "PENDING"
        raise VPError("QOJ did not expose a supported detailed failure verdict.")

    def seed_submissions(self, submissions: list[Submission]) -> None:
        for item in submissions:
            if item.verdict not in ("PENDING", "CORRECT", "TOO-LATE", "UNAVAILABLE"):
                self._verdict_cache[item.id] = (None, item.verdict, time.monotonic() + 300 + int(item.id) % 300)

    @staticmethod
    def reference_from_html(html: str, contest: Contest, elapsed: float, *, released: bool = False) -> Reference:
        raw_rows = js_value(html, "standings")
        raw_scores = js_value(html, "score")
        labels = js_value(html, "problems_id", list(contest.problems))
        if not isinstance(raw_rows, list) or not isinstance(raw_scores, dict):
            return Reference(message="QOJ did not provide the original scoreboard data.", released=released)
        if js_value(html, "icpc_partial_scores", False):
            return Reference(message="The original contest uses partial ICPC scores; its ranking rule is unsupported.", released=released)
        rows = []
        for raw in raw_rows:
            info = raw[2]
            # Imported category 3 may include original starred teams whose flags
            # were lost during import. Restore eligibility from the original archive.
            if len(info) < 4 or info[2] not in (3, 4):
                continue
            if info[0] not in raw_scores:
                return Reference(message="An original QOJ team is missing problem results.", released=released)
            cells = {label: Cell() for label in contest.problems}
            values = raw_scores.get(info[0], {})
            items = values.items() if isinstance(values, dict) else enumerate(values)
            for index, col in items:
                if not isinstance(col, list) or not str(index).isdigit() or int(index) >= len(labels) or len(col) < 6:
                    continue
                label = labels[int(index)]
                if label not in cells:
                    continue
                solved = col[0] == col[4] and col[4] > 0
                # QOJ stores attempts before the selected submission, even
                # when that last submission failed. Its own UI renders -N
                # and ?N as col[3] + 1, not col[3].
                cells[label] = (Cell(pending=int(col[3]) + 1) if col[5] else
                                Cell(int(col[3]) + 1, 0, solved, int(col[1] // 60) if solved else None))
            row = TeamScore(str(info[0]), info[3], cells, eligible=info[2] == 3,
                            penalty_minutes=contest.rules.penalty_minutes)
            if row.penalty != int(raw[1] // 60):
                return Reference(message="The original QOJ penalty data does not match the contest scoring rule.", released=released)
            rows.append(row)
        if not rows:
            return Reference(message="QOJ has no explicitly classified original contest ghosts.", released=released)
        medals = medals_from_html(html, contest.url + "/standings")
        # A mixed UCup/ghost scoreboard's cutoff describes its combined ranks.
        # Accept it only when every ranked row belongs to the original contest.
        if medals and len(rows) == len(raw_rows):
            contest.medals = medals
        final = elapsed >= contest.rules.duration and not contest.rules.frozen(elapsed, released)
        if not final:
            return Reference(message="Original scoreboard is final-only; live statistics are unavailable.", released=released)
        try:
            classify(rows, contest)
        except VPError as exc:
            return Reference(message=str(exc), released=released)
        return Reference(rows, complete=True, statistics_complete=True, released=released)

    def reference(self, contest: Contest, elapsed: float) -> Reference:
        soup = self._page("/standings", show_unofficial="true")
        html = str(soup)
        released = False
        if elapsed >= contest.rules.duration:
            # Require the current account's own native VP end/unfreeze evidence.
            my_name = js_value(html, "my_name", "")
            text = soup.get_text(" ", strip=True)
            native_ended = bool(re.search(r"Your (?:virtual (?:contest|participation)|participation in the contest)[^.]*?(?:has ended|has been finished)", text, re.I))
            if my_name == self.handle and native_ended:
                if self._finished_start != self.start_time:
                    current = self._registration(duration=contest.rules.duration)
                    if current and current[0] == self.start_time:
                        form = soup.select_one("form#form-finish-panel")
                        if form:
                            payload = form_values(form)
                            button = form.select_one('button[type="submit"][name], input[type="submit"][name]')
                            if not payload.get("_token") or button is None:
                                raise VPError("QOJ did not provide its native finished-VP update token.")
                            payload[button["name"]] = button.get("value", "")
                            action = urljoin(self.url, form.get("action") or self.url)
                            if urlsplit(action).hostname != urlsplit(self.origin).hostname:
                                raise VPError("QOJ returned an unexpected native finished-VP update destination.")
                            self.web.post(action, payload)
                            soup = self._page("/standings", show_unofficial="true")
                            html = str(soup)
                        self._finished_start = self.start_time
            own = js_value(html, "score", {}).get(self.handle, {})
            values = own.values() if isinstance(own, dict) else own
            no_hidden = all(isinstance(col, list) and len(col) >= 6 and not col[5] for col in values)
            released = (self.start_time is not None and self._finished_start == self.start_time
                        and js_value(html, "my_name", "") == self.handle and no_hidden)
        # Keep the public archive connection across metadata and event downloads.
        # This session carries none of the judge account's cookies.
        with requests.Session() as public_web:
            return self._original_reference(html, contest, elapsed, released, public_web,
                                            fetch_final=elapsed < contest.rules.duration)

    @staticmethod
    def external_reference_from_html(html: str, contest: Contest) -> Reference:
        """Read QOJ's public final table, matching only the original roster."""
        soup = BeautifulSoup(html, "html.parser")
        if "Current Time:" in soup.get_text(" ", strip=True):
            return Reference(message="QOJ returned a VP replay instead of the original final standings.")
        metadata = contest.extra["original_eligibility"]
        teams = eligibility_lookup(metadata["teams"])
        problems = list(contest.problems)
        for table in soup.select("table"):
            headers = table.select("tr th")
            labels = [node.get_text(" ", strip=True).split()[0] for node in headers]
            if labels[2:2 + len(problems)] != problems or labels[2 + len(problems):4 + len(problems)] != ["Solved", "Penalty"]:
                continue
            rows = []
            empty_duplicates = Counter(identity(name) for name in metadata.get("ignored_empty_team_names", []))
            for tr in table.select("tr"):
                columns = tr.select("td")
                if len(columns) != len(labels):
                    continue
                display_name = columns[1].get_text(" ", strip=True)
                team = lookup_team(teams, display_name)
                if team is None:
                    continue
                cells = {}
                for problem, node in zip(problems, columns[2:2 + len(problems)]):
                    value = node.get_text(" ", strip=True).lstrip("*")
                    accepted = re.fullmatch(r"\+(\d*)\s+(\d+):(\d{2})", value)
                    rejected = re.fullmatch(r"-(\d+)(?:\s+\d+:\d{2})?", value)
                    if accepted:
                        cells[problem] = Cell(int(accepted[1] or 0) + 1, 0, True,
                                              int(accepted[2]) * 60 + int(accepted[3]))
                    elif rejected:
                        cells[problem] = Cell(int(rejected[1]))
                    elif value == "-":
                        cells[problem] = Cell()
                    else:
                        return Reference(message="An original QOJ final result is frozen or unsupported.")
                row = TeamScore(team["id"], team["name"], cells, penalty_minutes=contest.rules.penalty_minutes)
                solved, penalty = (node.get_text(strip=True) for node in columns[2 + len(problems):4 + len(problems)])
                if not solved.isdigit() or not penalty.isdigit() or (row.solved, row.penalty) != (int(solved), int(penalty)):
                    return Reference(message="The public QOJ final table does not match its problem results.")
                name_key = identity(team["name"])
                if empty_duplicates[name_key] and not any(c.tries or c.pending or c.solved for c in cells.values()):
                    empty_duplicates[name_key] -= 1
                    continue
                rows.append(row)
            try:
                classify(rows, contest)
            except VPError as exc:
                return Reference(message=str(exc))
            return Reference(rows, complete=True, statistics_complete=True, released=True)
        return Reference(message="QOJ did not provide a public final table for these contest problems.")

    def _original_reference(self, html: str, contest: Contest, elapsed: float, released: bool, public_web,
                            *, fetch_final: bool = False) -> Reference:
        try:
            metadata = load_metadata(contest, web=public_web)
        except VPError as exc:
            return Reference(message=str(exc), released=released)
        contest.extra["original_eligibility"] = metadata
        duration = metadata.get("duration")
        if not isinstance(duration, int) or duration < contest.rules.duration:
            return Reference(message="The original contest duration is shorter than the native VP duration.", released=released)
        # An onsite extension can make the original longer than QOJ's native VP.
        # Validate the complete original using its own window; keep the user's
        # timer and the cutoff for their submissions at the native VP duration.
        original = replace(contest, rules=replace(contest.rules, duration=duration))
        if self._replay is not None and not fetch_final:
            return self._replay.at(contest, elapsed, released=released)
        # Imported ghosts contain final totals. Validate a separate original
        # event stream against those totals before exposing any partial scores.
        if fetch_final:
            # The authenticated standings are already truncated to VP time.
            # The public results endpoint has the complete imported scoreboard;
            # this separate session deliberately has no account cookies.
            if self._public_web is None:
                self._public_web = WebSession(self.origin, {})
            public = self._public_web.get(self.origin + "/results/QOJ" + self.id, params={"locale": "en"})
            final = self.external_reference_from_html(public.text, original)
        else:
            final = self.reference_from_html(html, original, duration, released=True)
        contest.medals = original.medals
        final.released = released
        if not final.complete:
            return final
        if elapsed >= contest.rules.duration and not contest.rules.frozen(elapsed, released):
            return final
        if self._replay is None and time.monotonic() >= self._next_replay_attempt:
            self._next_replay_attempt = time.monotonic() + 120
            try:
                events = load_archive_submissions(original, metadata, web=public_web)
                try:
                    replay = OriginalReplay(original, metadata["teams"], events, final.rows)
                except VPError:
                    offset = duration - contest.rules.duration
                    if offset <= 0:
                        raise
                    # Some QOJ imports shift the entire onsite clock. Accept
                    # the shift only if every team's every final cell agrees.
                    aligned = [replace(event, time=event.time - offset) for event in events]
                    replay = OriginalReplay(original, metadata["teams"], aligned, final.rows)
                self._replay = replay
                cache_archive_submissions(metadata, events)
            except VPError as exc:
                self._replay_error = str(exc)
        if self._replay is not None:
            return self._replay.at(contest, elapsed, released=released)
        return Reference(released=released, message=self._replay_error)
