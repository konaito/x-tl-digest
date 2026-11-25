"""digest.py のテストの土台。

外へ出る処理（X、Clef、claude -p、Discord、メール、Keychain）を全部にせものに差し替え、
DB・未送信メール・ロックの置き場を一時ディレクトリに移す。本物の Discord とメールには何も送らない。
"""

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import digest  # noqa: E402

FAKE_WEBHOOK = "http://127.0.0.1:9/fake-webhook"
REAL_DB = REPO / "data" / "digest.sqlite"


def tweet(i: int) -> dict:
    return {
        "id": str(i), "author": f"user{i}", "author_name": f"User {i}", "text": f"post {i}", "quoted_text": None,
        "lang": "ja", "created_at": "2026-11-25T00:00:00+00:00", "likes": 1, "retweets": 0, "views": 10,
        "urls": "[]", "fetched_at": digest.now_iso(),
    }


def base_config() -> dict:
    """本番の config.toml と同じキーだけを持つ設定。新しいキーが無くても動くことを、全テストが兼ねて確かめる。"""
    return {
        "fetch": {"pages": 5, "count": 20, "page_interval_sec": 3, "impersonate": "chrome124"},
        "clef": {"model": "@cf/cloudflare/clef", "account_id": "", "concurrency": 2,
                 "threshold_tech": 0.5, "threshold_worth": 0.5, "questions": {}},
        "summary": {"model": "", "max_candidates": 60, "max_trends": 8, "lookback_hours": 36, "profile": "読み手"},
        "mail": {"enabled": True, "to": ["agent@example.com"]},
    }


@pytest.fixture(scope="session", autouse=True)
def real_db_untouched():
    """手元に本番 DB の写しがあっても、テストがそれを書き換えていないことを最後に確かめる。"""
    before = REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else None
    yield
    after = REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else None
    assert before == after, "テストが本物の data/digest.sqlite に触った"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    state = SimpleNamespace(
        cfg=base_config(),
        fetches=[],          # fetch_timeline が順に返すもの。id の list か、投げる例外か、止まったままにする "hang"
        reject=set(),        # Clef がしきい値未満を返す id
        clef_error=False,    # 真なら Clef は全件失敗する
        summarize_error=False,
        no_trends=False,
        summaries=[],        # summarize に渡された候補の id（呼び出しごと）
        discord=[],          # post_discord に渡された payload
        mails=[],            # send_email に渡された (subject, body_text, body_html)
    )

    async def fake_fetch(fetch_cfg, pages):
        assert state.fetches, "想定していない TL 取得が呼ばれた"
        item = state.fetches.pop(0)
        if isinstance(item, Exception):
            raise item
        if item == "hang":
            await asyncio.sleep(60)
        return [tweet(i) for i in item]

    def fake_clef(clef_cfg, row):
        if state.clef_error:
            raise RuntimeError("clef down")
        p = 0.1 if row["id"] in state.reject else 0.9
        return {"is_tech": {"noul": p}, "worth": {"noul": p}, "importance": {"score": 2.0}}

    def fake_summarize(candidates, summary_cfg):
        state.summaries.append([r["id"] for r in candidates])
        if state.summarize_error:
            raise RuntimeError("claude down")
        if state.no_trends:
            return []
        return [{"title": "見出し", "summary": "要約", "take": "所感",
                 "tweet_ids": [candidates[0]["id"]], "source_urls": []}]

    def fake_post_discord(webhook, payload):
        assert webhook == FAKE_WEBHOOK
        state.discord.append(payload)
        return f"msg{len(state.discord)}"

    def fake_keychain_get(service):
        assert service == digest.WEBHOOK_SERVICE, f"想定していない Keychain の読み出し: {service}"
        return FAKE_WEBHOOK

    def fake_send_email(mail_cfg, subject, body_text, body_html):
        state.mails.append((subject, body_text, body_html))
        return "messageId=fake"

    def forbidden(*args, **kwargs):
        raise AssertionError("テストから外への通信や外部コマンドが呼ばれた")

    monkeypatch.setattr(digest, "ROOT", tmp_path)
    monkeypatch.setattr(digest, "DB_PATH", tmp_path / "data" / "digest.sqlite")
    monkeypatch.setattr(digest, "PENDING_DIR", tmp_path / "data" / "pending_mail")
    monkeypatch.setattr(digest, "LOCK_PATH", tmp_path / "data" / "digest.lock", raising=False)
    monkeypatch.setattr(digest, "load_config", lambda: state.cfg)
    monkeypatch.setattr(digest, "fetch_timeline", fake_fetch)
    monkeypatch.setattr(digest, "run_clef", fake_clef)
    monkeypatch.setattr(digest, "summarize", fake_summarize)
    monkeypatch.setattr(digest, "post_discord", fake_post_discord)
    monkeypatch.setattr(digest, "keychain_get", fake_keychain_get)
    monkeypatch.setattr(digest, "keychain_set", forbidden)
    monkeypatch.setattr(digest, "send_email", fake_send_email)
    monkeypatch.setattr(digest.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(digest.subprocess, "run", forbidden)
    monkeypatch.setattr(digest.subprocess, "check_output", forbidden)

    # 差し替わっていることを、どのテストも走る前に確かめる。
    assert digest.post_discord is fake_post_discord and digest.send_email is fake_send_email
    assert digest.keychain_get is fake_keychain_get and digest.fetch_timeline is fake_fetch
    for path in (digest.DB_PATH, digest.PENDING_DIR):
        assert tmp_path in path.parents and REPO not in path.parents

    def run(*flags: str) -> int:
        """digest.py を引数つきで1回動かし、終了コードを返す。"""
        monkeypatch.setattr(sys, "argv", ["digest.py", *flags])
        try:
            digest.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return 0

    def sql(query: str, *params) -> list[sqlite3.Row]:
        db = sqlite3.connect(digest.DB_PATH)
        db.row_factory = sqlite3.Row
        try:
            return db.execute(query, params).fetchall()
        finally:
            db.close()

    def execute(query: str, *params) -> None:
        db = sqlite3.connect(digest.DB_PATH)
        try:
            db.execute(query, params)
            db.commit()
        finally:
            db.close()

    state.run, state.sql, state.execute = run, sql, execute
    state.last_run = lambda: sql("SELECT * FROM runs ORDER BY id DESC LIMIT 1")[0]
    state.unconsumed = lambda: [r["id"] for r in sql("SELECT id FROM tweets WHERE digest_run IS NULL ORDER BY id")]
    return state
