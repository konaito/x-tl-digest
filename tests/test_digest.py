"""取得だけの回と送信の回の動きを、main() を通しで動かして確かめる。外へ出る処理は conftest.py がにせものにしている。"""

import fcntl
import json
import plistlib
import threading
import time
import tomllib
from pathlib import Path

import pytest

import digest

REPO = Path(__file__).resolve().parent.parent


def digests(env) -> list[dict]:
    """Discord に送られたもののうち、ダイジェスト本体（embed つき）。"""
    return [p for p in env.discord if p.get("embeds")]


def warnings(env) -> list[str]:
    """Discord に送られたもののうち、1行の警告。"""
    return [p["content"] for p in env.discord if not p.get("embeds")]


def header(env) -> str:
    return next(p["content"] for p in reversed(env.discord) if p.get("embeds") and "content" in p)


# --- 候補の消費 ---


def test_posted_run_consumes_overflow_candidates(env):
    env.cfg["summary"]["max_candidates"] = 3
    env.fetches = [[1, 2, 3, 4, 5]]

    assert env.run() == 0

    run = env.last_run()
    assert run["status"] == "posted"
    assert len(env.summaries[0]) == 3, "要約に渡すのは max_candidates まで"
    assert env.unconsumed() == [], "あふれた候補も消費して、次号に持ち越さない"
    assert {r["digest_run"] for r in env.sql("SELECT digest_run FROM tweets")} == {run["id"]}
    assert run["passed"] == 5
    assert "取得 1回・5件（新規 5件）→ 判定通過 5件（上位 3件を要約）→ 1トピック" in header(env)


def test_no_trends_run_consumes_overflow_candidates(env):
    env.cfg["summary"]["max_candidates"] = 3
    env.fetches = [[1, 2, 3, 4, 5]]
    env.no_trends = True

    assert env.run() == 0

    assert env.last_run()["status"] == "skipped"
    assert env.unconsumed() == []
    assert env.discord == []


def test_below_threshold_tweets_are_not_consumed(env):
    env.fetches = [[1, 2, 3]]
    env.reject = {"3"}

    assert env.run() == 0

    assert env.last_run()["status"] == "posted"
    assert env.unconsumed() == ["3"]


def test_failed_summary_consumes_nothing_and_next_run_retries(env):
    env.fetches = [[1, 2, 3], []]
    env.summarize_error = True

    assert env.run() == 1

    run = env.last_run()
    assert run["status"] == "failed" and "claude -p 要約" in run["error"]
    assert env.unconsumed() == ["1", "2", "3"]
    assert len(warnings(env)) == 1 and digests(env) == []

    env.summarize_error = False
    assert env.run() == 0
    assert env.last_run()["status"] == "posted"
    assert sorted(env.summaries[-1]) == ["1", "2", "3"], "失敗した回の候補を次の回が拾う"
    assert env.unconsumed() == []


def test_dry_run_consumes_nothing(env, capsys):
    env.fetches = [[1, 2]]

    assert env.run("--dry-run") == 0

    assert env.last_run()["status"] == "dry_run"
    assert env.unconsumed() == ["1", "2"]
    assert env.discord == [] and env.mails == []
    assert "XおすすめTLの技術トレンド" in capsys.readouterr().out


# --- ヘッダの件数 ---


def test_header_sums_fetches_since_last_send(env):
    env.fetches = [[1, 2, 3, 4], [5, 6, 7], [7, 8, 9], [10, 11]]
    assert env.run() == 0                       # 送信の回。ここが境目になる
    assert "取得 1回・4件（新規 4件）" in header(env)

    assert env.run("--no-summary") == 0         # 3件取得、新規3件
    assert env.run("--no-summary") == 0         # 3件取得、新規2件（7 は取得済み）
    assert env.run() == 0                       # 2件取得、新規2件

    assert "取得 3回・8件（新規 7件）→ 判定通過 7件 → 1トピック" in header(env)
    run = env.last_run()
    assert (run["fetched"], run["inserted"]) == (2, 2), "runs の行にはその回の取得分だけを残す"


def test_header_counts_collect_runs_and_ignores_runs_that_did_not_fetch(env):
    env.fetches = [[1, 2], [3, 4, 5], RuntimeError("x down"), [5, 6], [7], [8, 9]]
    assert env.run() == 0                       # 境目
    assert env.run("--collect") == 0            # 3件、新規3件
    assert env.run("--collect") == 1            # 取得に失敗。回数にも件数にも入らない
    assert env.run("--collect") == 0            # 2件、新規1件
    assert env.run("--dry-run") == 0            # 1件、新規1件。送らないが取得はしている
    assert env.run("--no-summary", "--no-fetch") == 0   # 取得していない
    # 途中で落ちて件数が入らなかった回が残っていても数えない。
    env.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", digest.now_iso())

    assert env.run() == 0                       # 2件、新規2件

    assert "取得 4回・8件（新規 7件）→ 判定通過 7件 → 1トピック" in header(env)
    assert [r["status"] for r in env.sql("SELECT status FROM runs ORDER BY id")] == [
        "posted", "collected", "collect_failed", "collected", "dry_run", "classified_only", "running", "posted",
    ]


def test_dry_run_without_candidates_does_not_become_a_boundary(env):
    env.fetches = [[1, 2], [3, 4], [5, 6]]
    env.reject = {"3", "4"}
    assert env.run() == 0                       # 境目
    assert env.run("--no-summary") == 0         # 2件取得、どれも通過しない

    assert env.run("--dry-run", "--no-fetch") == 0   # 候補が無い dry-run
    assert env.last_run()["status"] == "dry_run", "dry-run は skipped を書かない（skipped は送信の回の境目）"

    assert env.run() == 0
    assert "取得 2回・4件（新規 4件）" in header(env)


def test_dry_run_without_trends_does_not_become_a_boundary(env):
    env.fetches = [[1, 2]]
    env.no_trends = True

    assert env.run("--dry-run") == 0

    assert env.last_run()["status"] == "dry_run"
    assert env.unconsumed() == ["1", "2"]


def test_skipped_run_is_a_boundary(env):
    env.fetches = [[1, 2], [3]]
    env.reject = {"1", "2"}
    assert env.run() == 0                       # 候補が無いので送らない
    assert env.last_run()["status"] == "skipped"

    assert env.run() == 0
    assert "取得 1回・1件（新規 1件）" in header(env)


# --- 送信の回は欠号させない ---


def test_send_run_survives_fetch_failure(env):
    env.fetches = [[1, 2, 3], RuntimeError("401 Unauthorized")]
    assert env.run("--no-summary") == 0

    assert env.run() == 0

    run = env.last_run()
    assert run["status"] == "posted"
    assert "TL取得" in run["error"] and "401 Unauthorized" in run["error"]
    assert sorted(env.summaries[-1]) == ["1", "2", "3"], "DB にたまっていた分で要約する"
    assert env.unconsumed() == []
    head = header(env)
    assert "取得 1回・3件（新規 3件）" in head
    assert "TL取得に失敗" in head and "401 Unauthorized" in head
    assert warnings(env) == [], "失敗はダイジェストのヘッダに書くので、別の警告は出さない"
    (subject, body_text, body_html), = env.mails
    assert "401 Unauthorized" not in body_text and "401 Unauthorized" not in body_html, "警告はメールに入れない"
    assert "取得 1回・3件（新規 3件）" in body_text


def test_send_run_survives_a_fetch_that_never_returns(env, monkeypatch):
    monkeypatch.setattr(digest, "FETCH_TIMEOUT_SEC", 0.05)
    env.fetches = [[1, 2], "hang"]
    assert env.run("--no-summary") == 0

    assert env.run() == 0

    run = env.last_run()
    assert run["status"] == "posted" and "TL取得: 0.05 秒待っても終わらなかった" in run["error"]
    assert sorted(env.summaries[-1]) == ["1", "2"]


def test_dry_run_records_the_fetch_failure(env):
    env.fetches = [[1, 2], RuntimeError("401 Unauthorized")]
    assert env.run("--no-summary") == 0

    assert env.run("--dry-run") == 0

    run = env.last_run()
    assert run["status"] == "dry_run" and "TL取得: 401 Unauthorized" in run["error"]
    assert env.discord == [] and env.unconsumed() == ["1", "2"]


def test_send_run_survives_classify_failure(env):
    env.fetches = [[1, 2], [3, 4]]
    assert env.run("--no-summary") == 0
    env.clef_error = True

    assert env.run() == 0

    run = env.last_run()
    assert run["status"] == "posted" and "Clef判定" in run["error"]
    assert sorted(env.summaries[-1]) == ["1", "2"]
    assert "Clef判定に失敗" in header(env)
    assert env.unconsumed() == ["3", "4"], "判定できなかった投稿は残し、次の回に判定する"


def test_send_run_fails_when_fetch_fails_and_nothing_is_stored(env):
    env.fetches = [RuntimeError("401 Unauthorized")]

    assert env.run() == 1

    run = env.last_run()
    assert run["status"] == "failed" and "TL取得" in run["error"]
    assert env.summaries == [] and digests(env) == []
    assert len(warnings(env)) == 1 and "TL取得" in warnings(env)[0]


def test_send_run_reports_fetch_failure_when_there_are_no_trends(env):
    env.fetches = [[1, 2], RuntimeError("401 Unauthorized")]
    assert env.run("--no-summary") == 0
    env.no_trends = True

    assert env.run() == 0

    run = env.last_run()
    assert run["status"] == "skipped" and "TL取得" in run["error"]
    assert digests(env) == []
    assert len(warnings(env)) == 1 and "TL取得" in warnings(env)[0]


def test_mail_failure_keeps_the_run_posted_and_saves_the_mail(env, monkeypatch):
    def broken_send_email(*args):
        raise RuntimeError("mail api down")

    monkeypatch.setattr(digest, "send_email", broken_send_email)
    env.fetches = [[1, 2]]

    assert env.run() == 0

    run = env.last_run()
    assert run["status"] == "posted" and run["error"] == "メール送信: mail api down"
    assert env.unconsumed() == []
    assert len(list(digest.PENDING_DIR.glob("*.json"))) == 1
    assert len(digests(env)) == 1 and len(warnings(env)) == 1 and "メール送信失敗" in warnings(env)[0]


def test_dry_run_never_posts_even_on_failure(env):
    env.fetches = [RuntimeError("401 Unauthorized")]

    assert env.run("--dry-run") == 1

    assert env.last_run()["status"] == "failed"
    assert env.discord == [] and env.mails == []


# --- 取得だけの回 ---


def test_collect_fetches_and_classifies_only(env):
    env.fetches = [[1, 2, 3]]
    env.reject = {"3"}

    assert env.run("--collect") == 0

    run = env.last_run()
    assert run["status"] == "collected"
    assert (run["fetched"], run["inserted"], run["classified"], run["passed"]) == (3, 3, 3, 2)
    assert run["error"] is None and run["finished_at"]
    assert env.summaries == [] and env.discord == [] and env.mails == []
    assert env.unconsumed() == ["1", "2", "3"], "取得だけの回は候補を消費しない"
    assert env.sql("SELECT COUNT(*) n FROM tweets WHERE classified_at IS NOT NULL")[0]["n"] == 3


def test_collect_failure_warns_once_at_three_in_a_row(env):
    env.fetches = [RuntimeError("401 Unauthorized")] * 5

    for expected_warnings in (0, 0, 1, 1, 1):
        assert env.run("--collect") == 1
        run = env.last_run()
        assert run["status"] == "collect_failed" and "TL取得" in run["error"]
        assert len(warnings(env)) == expected_warnings
    assert "3回" in warnings(env)[0] and "401 Unauthorized" in warnings(env)[0]

    env.fetches = [[1]] + [RuntimeError("403 Forbidden")] * 4
    assert env.run("--collect") == 0            # 1回成功すると数え直す
    for expected_warnings in (1, 1, 2, 2):
        assert env.run("--collect") == 1
        assert len(warnings(env)) == expected_warnings
    assert digests(env) == [] and env.mails == []


def test_send_runs_do_not_count_toward_the_collect_failure_streak(env):
    env.fetches = [RuntimeError("down")] * 4
    assert env.run("--collect") == 1
    assert env.run("--collect") == 1
    assert env.run() == 1                       # 送信の回の失敗は、今までどおり毎回1行出す
    assert len(warnings(env)) == 1
    assert "3回" not in warnings(env)[0]

    assert env.run("--collect") == 1            # 取得だけの回としては3回目
    assert len(warnings(env)) == 2 and "3回" in warnings(env)[1]


def test_collect_classify_failure_is_recorded(env):
    env.fetches = [[1, 2]]
    env.clef_error = True

    assert env.run("--collect") == 1

    run = env.last_run()
    assert run["status"] == "collect_failed" and "Clef判定" in run["error"]
    assert (run["fetched"], run["inserted"]) == (2, 2), "取得できた分は残す"
    assert env.discord == []


def test_collect_unexpected_error_is_recorded_without_posting(env):
    env.fetches = [[1, 2]]
    del env.cfg["clef"]["threshold_tech"]       # 設定の欠けのような、取得と判定の外で起きる失敗

    assert env.run("--collect") == 1

    run = env.last_run()
    assert run["status"] == "collect_failed" and "threshold_tech" in run["error"]
    assert env.discord == []


def test_collect_does_not_resend_pending_mail(env):
    digest.PENDING_DIR.mkdir(parents=True)
    pending = digest.PENDING_DIR / "20261125-100000.json"
    pending.write_text(json.dumps({"subject": "s", "body_text": "t", "body_html": "h"}))
    env.fetches = [[1], [2]]

    assert env.run("--collect") == 0
    assert env.mails == [] and pending.exists(), "取得だけの回はメールを送らない"

    assert env.run() == 0
    assert env.mails[0] == ("s", "t", "h") and not pending.exists(), "送信の回が再送する"


@pytest.mark.parametrize("flags", [("--collect", "--dry-run"), ("--collect", "--no-summary")])
def test_collect_rejects_flags_of_the_send_run(env, flags):
    assert env.run(*flags) == 2
    assert not digest.DB_PATH.exists()


# --- 同時実行の防止 ---


@pytest.fixture
def held_lock():
    digest.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(digest.LOCK_PATH, "w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield f
    f.close()


def test_collect_exits_quietly_while_another_run_holds_the_lock(env, held_lock):
    assert env.run("--collect") == 0

    assert not digest.DB_PATH.exists(), "runs に行を残さない"
    assert env.discord == []


def test_send_run_gives_up_with_a_warning_when_the_lock_never_frees(env, held_lock, monkeypatch):
    monkeypatch.setattr(digest, "SEND_LOCK_WAIT_SEC", 0)

    assert env.run() == 1

    assert not digest.DB_PATH.exists()
    assert len(warnings(env)) == 1 and "別の実行" in warnings(env)[0]


def test_dry_run_gives_up_without_posting_when_the_lock_never_frees(env, held_lock, monkeypatch):
    monkeypatch.setattr(digest, "SEND_LOCK_WAIT_SEC", 0)

    assert env.run("--dry-run") == 1

    assert env.discord == []


def test_send_run_waits_for_the_lock(env, held_lock, monkeypatch):
    monkeypatch.setattr(digest, "SEND_LOCK_WAIT_SEC", 10)
    monkeypatch.setattr(digest, "LOCK_POLL_SEC", 0.02)
    env.fetches = [[1, 2]]
    threading.Timer(0.2, held_lock.close).start()

    started = time.monotonic()
    assert env.run() == 0

    assert time.monotonic() - started >= 0.2
    assert env.last_run()["status"] == "posted"


def test_lock_is_released_after_each_run(env):
    env.fetches = [[1], [2]]
    assert env.run("--collect") == 0
    assert env.run("--collect") == 0
    assert env.sql("SELECT COUNT(*) n FROM runs WHERE status = 'collected'")[0]["n"] == 2


# --- 設定と launchd ---


def test_example_config_keeps_the_agreed_limits():
    cfg = tomllib.loads((REPO / "config.example.toml").read_text())
    assert cfg["summary"]["max_candidates"] == 100
    assert cfg["summary"]["max_trends"] == 8


def load_plist(name: str) -> dict:
    text = (REPO / "launchd" / name).read_text()
    text = text.replace("__LABEL__", "local.x-tl-digest").replace("__ROOT__", "/r").replace("__HOME__", "/h")
    return plistlib.loads(text.encode())


def test_launchd_schedules_do_not_overlap():
    send = load_plist("x-tl-digest.plist")
    collect = load_plist("x-tl-digest-collect.plist")

    assert send["Label"] == "local.x-tl-digest"
    assert collect["Label"] == "local.x-tl-digest-collect"
    assert "--collect" not in send["ProgramArguments"]
    assert collect["ProgramArguments"] == send["ProgramArguments"] + ["--collect"]
    assert collect["StandardOutPath"] == send["StandardOutPath"] == "/r/logs/digest.log"

    send_times = [(t["Hour"], t["Minute"]) for t in send["StartCalendarInterval"]]
    collect_times = [(t["Hour"], t["Minute"]) for t in collect["StartCalendarInterval"]]
    assert send_times == [(10, 0), (20, 0)]
    assert collect_times == [(h, 0) for h in (0, 2, 4, 6, 8, 12, 14, 16, 18, 22)]
