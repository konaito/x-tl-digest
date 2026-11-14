"""XのおすすめTLから技術トレンドを拾い、Clef で絞り、claude -p で要約して Discord とメールで送る。

launchd から 10:00 と 20:00 に1回ずつ起動する想定。設定は config.toml（config.example.toml を写して作る）。
"""

import argparse
import asyncio
import html
import json
import os
import sqlite3
import subprocess
import sys
import time
import tomllib
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from twikit import Client

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "digest.sqlite"
JST = timezone(timedelta(hours=9))
COOKIE_SERVICE = "x-tl-digest.x-cookies"
WEBHOOK_SERVICE = "x-tl-digest.discord-webhook"
# メール送信 API（Google Apps Script の Web アプリ）の URL と token。リポジトリは公開なので値は Keychain にだけ置く。
MAIL_URL_SERVICE = "claudecode-mail-proxy.url"
MAIL_TOKEN_SERVICE = "claudecode-mail-proxy.token"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets (
  id TEXT PRIMARY KEY,
  author TEXT,
  author_name TEXT,
  text TEXT,
  quoted_text TEXT,
  lang TEXT,
  created_at TEXT,
  likes INTEGER,
  retweets INTEGER,
  views INTEGER,
  urls TEXT,
  fetched_at TEXT,
  p_tech REAL,
  p_worth REAL,
  importance REAL,
  classified_at TEXT,
  digest_run INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  fetched INTEGER,
  inserted INTEGER,
  classified INTEGER,
  passed INTEGER,
  trends INTEGER,
  discord_message_ids TEXT,
  error TEXT
);
"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "trends": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "take": {"type": "string"},
                    "tweet_ids": {"type": "array", "items": {"type": "string"}},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "summary", "take", "tweet_ids", "source_urls"],
            },
        }
    },
    "required": ["trends"],
}

SUMMARY_PROMPT = """XのおすすめTLに流れてきた投稿から、読み手が理解しておくべき技術トレンドを整理してください。

読み手: {profile}

下のJSONは、今回TLに流れてきた投稿のうち、判定モデルが「技術トレンドとして知っておく価値がある」と判定したものです。

やること:
1. 同じ話題の投稿をまとめ、読み手が理解しておくべきトレンドを最大{max_trends}件、重要な順に並べる。
2. 宣伝だけの投稿、感想だけの投稿、トレンドと言えない単発の小ネタは捨てる。該当がなければ trends を空配列にする。
3. 各トレンドに次を書く。
   - title: 30字以内の見出し
   - summary: 何が起きたか、何が新しいかを2〜4文で。投稿に書かれている事実だけを使い、投稿にない数値や固有名詞を足さない。
   - take: このダイジェストを書いているあなた（AI）自身の所感を、一人称で1〜3文。読み手の開発を日々手伝う立場として、この話題であなたが読み手を手伝える力が変わるかを率直に書く。すでに知っていたことか初めて知ったことか、読み手の作業を手伝うときに具体的に何を変えるか、または何も変わらないかを書く。変わらないなら「自分には変化なし」と書いてよい。読み手がなぜ知るべきかの説明や、読み手への勧めは書かない。
   - tweet_ids: 根拠になる主要ツイートの id を重要な順に最大3件。入力にある id だけを使う。
   - source_urls: NotebookLM に入れて深掘りできる外部URL。入力の urls にあるものだけを使い、x.com と twitter.com のURLは除く。なければ空配列。
4. 日本語の常体で書く。

投稿:
{posts}
"""


def log(msg: str) -> None:
    print(f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Keychain の項目のアカウント名。config.toml の keychain_account で変えられ、省くとログインユーザー名を使う。
KEYCHAIN_ACCOUNT = os.environ.get("USER", "")


def retry(fn, attempts: int = 3, delay: float = 5.0):
    """fn を最大 attempts 回試す。失敗の間は delay 秒空ける。最後の失敗はそのまま投げる。"""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1:
                raise
            log(f"リトライ {i + 1}/{attempts - 1}: {one_line(str(e), 120)}")
            time.sleep(delay)


def keychain_get(service: str) -> str:
    return retry(lambda: subprocess.check_output(
        ["security", "find-generic-password", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w"], text=True
    ).strip())


def keychain_set(service: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w", value], check=True
    )


def load_config() -> dict:
    path = ROOT / "config.toml"
    if not path.exists():
        sys.exit("config.toml がない。config.example.toml を config.toml に写して書き換える。")
    return tomllib.loads(path.read_text())


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# --- 取得 ---


def to_row(tweet, fetched_at: str) -> dict:
    # RT は元ツイートの id と本文で持つ。同じ投稿が RT 経由で何度流れてきても1件にまとまる。
    src = tweet.retweeted_tweet or tweet
    quote = src.quote
    urls = []
    for t in (src, quote):
        for u in (t.urls or []) if t else []:
            url = u.get("expanded_url") if isinstance(u, dict) else u
            if url and url not in urls:
                urls.append(url)
    views = src.view_count
    return {
        "id": src.id,
        "author": src.user.screen_name,
        "author_name": src.user.name,
        "text": src.full_text,
        "quoted_text": f"@{quote.user.screen_name}: {quote.full_text}" if quote else None,
        "lang": src.lang,
        "created_at": datetime.strptime(src.created_at, "%a %b %d %H:%M:%S %z %Y").isoformat(),
        "likes": src.favorite_count,
        "retweets": src.retweet_count,
        "views": int(views) if views else None,
        "urls": json.dumps(urls, ensure_ascii=False),
        "fetched_at": fetched_at,
    }


async def fetch_timeline(fetch_cfg: dict, pages: int) -> list[dict]:
    cookies = json.loads(keychain_get(COOKIE_SERVICE))
    client = Client("ja-JP", impersonate=fetch_cfg["impersonate"] or None)
    client.set_cookies(cookies)

    page = await client.get_timeline(count=fetch_cfg["count"])
    tweets = list(page)
    for _ in range(pages - 1):
        if not page.next_cursor or len(page) == 0:
            break
        await asyncio.sleep(fetch_cfg["page_interval_sec"])
        page = await page.next()
        tweets.extend(page)

    # ct0 はXが更新することがあるので、変わっていたら Keychain に書き戻す。
    updated = client.get_cookies()
    if updated.get("auth_token") and updated.get("ct0") and updated["ct0"] != cookies["ct0"]:
        keychain_set(COOKIE_SERVICE, json.dumps({"auth_token": updated["auth_token"], "ct0": updated["ct0"]}))
        log("ct0 が更新されたので Keychain に保存した")

    fetched_at = now_iso()
    rows = []
    for t in tweets:
        try:
            rows.append(to_row(t, fetched_at))
        except Exception as e:  # 削除済みや非公開の投稿が混ざっても、残りは処理する
            log(f"投稿 {getattr(t, 'id', '?')} を読めなかったので飛ばした: {e!r}")
    return rows


def insert_tweets(db: sqlite3.Connection, rows: list[dict]) -> int:
    before = db.total_changes
    db.executemany(
        """INSERT OR IGNORE INTO tweets
           (id, author, author_name, text, quoted_text, lang, created_at, likes, retweets, views, urls, fetched_at)
           VALUES (:id, :author, :author_name, :text, :quoted_text, :lang, :created_at, :likes, :retweets, :views,
                   :urls, :fetched_at)""",
        rows,
    )
    db.commit()
    return db.total_changes - before


# --- Clef で判定 ---


def clef_request(clef_cfg: dict, row: sqlite3.Row) -> dict:
    state = {"author": f"@{row['author']}", "text": row["text"]}
    if row["quoted_text"]:
        state["quoted_post"] = row["quoted_text"]
    links = json.loads(row["urls"])
    if links:
        state["links"] = links
    return {
        "model": clef_cfg["model"].rsplit("/", 1)[1],
        "state": state,
        "questions": clef_cfg["questions"],
    }


def run_clef(clef_cfg: dict, row: sqlite3.Row) -> dict:
    body = json.dumps(clef_request(clef_cfg, row), ensure_ascii=False)
    env = dict(os.environ)
    if clef_cfg.get("account_id"):
        env["CLOUDFLARE_ACCOUNT_ID"] = clef_cfg["account_id"]
    last_error = ""
    for attempt in range(3):
        proc = subprocess.run(
            ["cf", "ai", "run", clef_cfg["model"], "--body", body],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)["answers"]
        last_error = (proc.stderr or proc.stdout)[-400:]
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"cf ai run が3回失敗: {last_error}")


def classify(db: sqlite3.Connection, clef_cfg: dict) -> tuple[int, int]:
    rows = db.execute("SELECT * FROM tweets WHERE classified_at IS NULL").fetchall()
    if not rows:
        return 0, 0
    done, failed, errors = 0, 0, []
    with ThreadPoolExecutor(max_workers=clef_cfg["concurrency"]) as pool:
        futures = {pool.submit(run_clef, clef_cfg, row): row["id"] for row in rows}
        for fut in as_completed(futures):
            try:
                ans = fut.result()
            except Exception as e:  # 1件の失敗で全体を止めず、次回に再判定する
                failed += 1
                errors.append(str(e))
                continue
            db.execute(
                "UPDATE tweets SET p_tech = ?, p_worth = ?, importance = ?, classified_at = ? WHERE id = ?",
                (ans["is_tech"]["noul"], ans["worth"]["noul"], ans["importance"]["score"], now_iso(), futures[fut]),
            )
            done += 1
    db.commit()
    if done == 0:
        raise RuntimeError(f"Clef の判定が全件失敗した（{failed}件）: {errors[0]}")
    if failed:
        log(f"Clef の判定に {failed} 件失敗した。次回に再判定する: {errors[0]}")
    return done, failed


def select_candidates(db: sqlite3.Connection, clef_cfg: dict, summary_cfg: dict) -> list[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(hours=summary_cfg["lookback_hours"])).isoformat(timespec="seconds")
    return db.execute(
        """SELECT * FROM tweets
           WHERE digest_run IS NULL AND classified_at IS NOT NULL AND fetched_at >= ?
             AND p_tech >= ? AND p_worth >= ?
           ORDER BY importance DESC, p_worth DESC
           LIMIT ?""",
        (since, clef_cfg["threshold_tech"], clef_cfg["threshold_worth"], summary_cfg["max_candidates"]),
    ).fetchall()


# --- claude -p で要約 ---


def summarize(candidates: list[sqlite3.Row], summary_cfg: dict) -> list[dict]:
    posts = [
        {
            "id": r["id"],
            "author": f"@{r['author']}",
            "text": r["text"][:1500],
            "quoted_post": r["quoted_text"][:800] if r["quoted_text"] else None,
            "urls": json.loads(r["urls"]),
            "likes": r["likes"],
            "views": r["views"],
        }
        for r in candidates
    ]
    prompt = SUMMARY_PROMPT.format(
        profile=summary_cfg["profile"],
        max_trends=summary_cfg["max_trends"],
        posts=json.dumps(posts, ensure_ascii=False, indent=1),
    )
    # --setting-sources project でユーザー設定の hook（日記・通知など）を読み込ませない。
    cmd = [
        "claude", "-p",
        "--setting-sources", "project",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(SUMMARY_SCHEMA),
    ]
    if summary_cfg["model"]:
        cmd += ["--model", summary_cfg["model"]]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900, cwd=ROOT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p が終了コード {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}")
    result = json.loads(proc.stdout)
    if result.get("is_error") or not result.get("structured_output"):
        raise RuntimeError(f"claude -p がエラーを返した: {str(result.get('result'))[:400]}")

    # リンクと投稿者は必ず DB の値から作るので、入力にない id や URL はここで捨てる。
    ids = {r["id"] for r in candidates}
    known_urls = {u for r in candidates for u in json.loads(r["urls"])}
    trends = []
    for t in result["structured_output"]["trends"]:
        t["tweet_ids"] = [i for i in t["tweet_ids"] if i in ids][:3]
        t["source_urls"] = [
            u for u in t["source_urls"] if u in known_urls and "://x.com/" not in u and "://twitter.com/" not in u
        ]
        if t["tweet_ids"]:
            trends.append(t)
    return trends


# --- Discord ---


def clip_lines(lines: list[str], limit: int) -> str:
    out, size = [], 0
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def one_line(text: str, n: int) -> str:
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build_messages(header: str, trends: list[dict], rows: dict[str, sqlite3.Row]) -> list[dict]:
    embeds = []
    for i, t in enumerate(trends, 1):
        tweet_lines = []
        for tid in t["tweet_ids"]:
            r = rows[tid]
            tweet_lines.append(f"[@{r['author']}](https://x.com/{r['author']}/status/{tid}) {one_line(r['text'], 70)}")
        fields = [{"name": "主要ツイート", "value": clip_lines(tweet_lines, 1024)}]
        if t["source_urls"]:
            fields.append({"name": "NotebookLM用ソース", "value": clip_lines(t["source_urls"], 1024)})
        first = rows[t["tweet_ids"][0]]
        description = f"{t['summary']}\n\n**AIの所感**\n{t['take']}"
        embeds.append({
            "title": one_line(f"{i}. {t['title']}", 256),
            "url": f"https://x.com/{first['author']}/status/{first['id']}",
            "description": description[:4096],
            "fields": fields,
            "color": 0x1D9BF0,
        })

    # 1メッセージは embed 10個・合計6000字まで。2026-10-23 以降、日本語8件・5386字（UTF-8 で 10374 バイト）が
    # 413 になり、5996 バイトの分は通った。字数でなくバイト数で数えている可能性があるので、バイト数で余裕を持って切る。
    messages, current, size = [], [], 0
    for e in embeds:
        parts = [e["title"], e["description"]] + [s for f in e["fields"] for s in (f["name"], f["value"])]
        e_size = sum(len(s.encode()) for s in parts)
        if current and (len(current) == 10 or size + e_size > 5000):
            messages.append(current)
            current, size = [], 0
        current.append(e)
        size += e_size
    if current:
        messages.append(current)
    payloads = [{"embeds": m, "allowed_mentions": {"parse": []}} for m in messages]
    payloads[0]["content"] = header
    return payloads


def post_discord(webhook: str, payload: dict) -> str:
    req = urllib.request.Request(
        webhook + "?wait=true",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "x-tl-digest"},
    )
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read())["id"]
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                time.sleep(float(json.loads(body).get("retry_after", 2)))
                continue
            raise RuntimeError(f"Discord {e.code}: {body[:300]}")
    raise RuntimeError("Discord のレート制限が解けなかった")


def post_messages(webhook: str, payloads: list[dict]) -> list[str]:
    ids = []
    for p in payloads:
        try:
            ids.append(post_discord(webhook, p))
        except RuntimeError as e:
            # サイズ超過（413）なら embed を半分に分けて送り直す。
            embeds = p.get("embeds", [])
            if "Discord 413" not in str(e) or len(embeds) < 2:
                raise
            half = len(embeds) // 2
            first = {**p, "embeds": embeds[:half]}
            rest = {k: v for k, v in p.items() if k != "content"} | {"embeds": embeds[half:]}
            ids += post_messages(webhook, [first, rest])
    return ids


# --- メール ---


def build_email(trends: list[dict], rows: dict[str, sqlite3.Row], stats_line: str) -> tuple[str, str, str]:
    """件名・プレーンテキスト・HTML を返す。メールクライアントで崩れないよう、HTML はインラインCSSだけで書く。"""
    now = datetime.now(JST)
    subject = f"XおすすめTLの技術トレンド {now:%m/%d %H:%M}（{len(trends)}トピック）"
    esc = html.escape
    text_parts = [f"{subject}\n{stats_line}\n"]
    blocks = []
    for i, t in enumerate(trends, 1):
        first = rows[t["tweet_ids"][0]]
        first_url = f"https://x.com/{first['author']}/status/{first['id']}"
        tweets = [(rows[tid], f"https://x.com/{rows[tid]['author']}/status/{tid}") for tid in t["tweet_ids"]]

        text_parts.append(
            f"{'=' * 40}\n{i}. {t['title']}\n{first_url}\n\n{t['summary']}\n\n[AIの所感]\n{t['take']}\n\n[主要ツイート]\n"
            + "\n".join(f"@{r['author']} {one_line(r['text'], 80)}\n  {u}" for r, u in tweets)
            + ("\n\n[ソース]\n" + "\n".join(t["source_urls"]) if t["source_urls"] else "")
            + "\n"
        )

        tweet_items = "".join(
            f'<li style="margin:0 0 6px"><a href="{esc(u)}" style="color:#1d4ed8;text-decoration:none">@{esc(r["author"])}</a> '
            f'<span style="color:#475569">{esc(one_line(r["text"], 90))}</span></li>'
            for r, u in tweets
        )
        source_html = ""
        if t["source_urls"]:
            source_items = "".join(
                f'<li style="margin:0 0 4px;word-break:break-all"><a href="{esc(u)}" style="color:#1d4ed8">{esc(u)}</a></li>'
                for u in t["source_urls"]
            )
            source_html = (
                '<p style="margin:14px 0 4px;font-size:12px;color:#64748b;font-weight:bold">ソース</p>'
                f'<ul style="margin:0;padding-left:18px;font-size:13px">{source_items}</ul>'
            )
        blocks.append(
            '<div style="padding:20px 0;border-top:1px solid #e2e8f0">'
            f'<h2 style="margin:0 0 10px;font-size:18px;line-height:1.4">'
            f'<a href="{esc(first_url)}" style="color:#0f172a;text-decoration:none">{i}. {esc(t["title"])}</a></h2>'
            f'<p style="margin:0 0 12px;font-size:15px;line-height:1.75;color:#1e293b">{esc(t["summary"])}</p>'
            '<div style="margin:0 0 12px;padding:10px 14px;background:#f8fafc;border-left:3px solid #94a3b8">'
            '<p style="margin:0 0 4px;font-size:12px;color:#64748b;font-weight:bold">AIの所感</p>'
            f'<p style="margin:0;font-size:14px;line-height:1.7;color:#334155">{esc(t["take"])}</p></div>'
            '<p style="margin:0 0 4px;font-size:12px;color:#64748b;font-weight:bold">主要ツイート</p>'
            f'<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.6">{tweet_items}</ul>'
            f"{source_html}</div>"
        )

    body_html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;background:#ffffff">'
        '<div style="max-width:640px;margin:0 auto;padding:24px 16px;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',\'Noto Sans JP\',sans-serif">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#0f172a">XおすすめTLの技術トレンド</h1>'
        f'<p style="margin:0 0 8px;font-size:13px;color:#64748b">{now:%Y/%m/%d %H:%M} ・ {esc(stats_line)}</p>'
        + "".join(blocks)
        + '<p style="margin:16px 0 0;padding-top:12px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8">'
        "x-tl-digest が自動で送っています。</p>"
        "</div></body></html>"
    )
    return subject, "\n".join(text_parts), body_html


def _send_email_once(mail_cfg: dict, subject: str, body_text: str, body_html: str) -> str:
    """メール送信 API に POST する。成功は {"ok": true, ...}。JSON でない応答や ok が真でない応答は失敗にする。"""
    payload = {"token": keychain_get(MAIL_TOKEN_SERVICE), "to": ",".join(mail_cfg["to"]),
               "subject": subject, "html": body_html, "text": body_text}
    if mail_cfg.get("name"):
        payload["name"] = mail_cfg["name"]
    req = urllib.request.Request(
        keychain_get(MAIL_URL_SERVICE), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    # GAS は 302 を挟んで最後に 200 を返す。urllib は 302 を追い、2回目は GET になる（GAS の結果の取得はそれでよい）。
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"メール送信 API が HTTP {e.code}: {one_line(e.read().decode('utf-8', 'replace'), 200)}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"メール送信 API に接続できない: {e.reason}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"メール送信 API が JSON でない応答を返した: {one_line(raw, 200)}") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        err = data.get("error") if isinstance(data, dict) else data
        raise RuntimeError(f"メール送信 API が失敗を返した: {one_line(str(err), 200)}")
    return f"messageId={data.get('messageId')} from={data.get('from')}"


def send_email(mail_cfg: dict, subject: str, body_text: str, body_html: str) -> str:
    return retry(lambda: _send_email_once(mail_cfg, subject, body_text, body_html))


PENDING_DIR = ROOT / "data" / "pending_mail"


def save_pending_email(mail_cfg: dict, email: tuple[str, str, str]) -> Path:
    """送れなかったメールを保存する。次回実行時に resend_pending_emails が再送する。"""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = PENDING_DIR / f"{datetime.now(JST):%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(
        {"subject": email[0], "body_text": email[1], "body_html": email[2], "mail_cfg": mail_cfg},
        ensure_ascii=False,
    ))
    return path


def resend_pending_emails(mail_cfg: dict) -> None:
    """未送信のメールを古い順に再送する。成功したファイルは削除、失敗したら残す。"""
    for path in sorted(PENDING_DIR.glob("*.json")) if PENDING_DIR.exists() else []:
        try:
            d = json.loads(path.read_text())
            result = send_email(d.get("mail_cfg") or mail_cfg, d["subject"], d["body_text"], d["body_html"])
            path.unlink()
            log(f"未送信メール再送: {path.name} {result}")
        except Exception as e:
            log(f"未送信メール再送に失敗（残す）: {path.name}: {one_line(str(e), 200)}")
            return


# --- main ---


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Discord に送らず、送る内容を表示する")
    parser.add_argument("--no-fetch", action="store_true", help="TL を取得せず、DB にある分だけで処理する")
    parser.add_argument("--no-summary", action="store_true", help="判定までで止める（しきい値の調整用）")
    parser.add_argument("--pages", type=int, help="config.toml の fetch.pages を上書きする")
    parser.add_argument("--email-preview", type=Path, help="--dry-run のとき、メールの HTML をこのパスに書き出す")
    args = parser.parse_args()

    global KEYCHAIN_ACCOUNT
    cfg = load_config()
    KEYCHAIN_ACCOUNT = cfg.get("keychain_account") or KEYCHAIN_ACCOUNT
    db = open_db()
    if not args.dry_run and cfg.get("mail", {}).get("enabled"):
        try:
            resend_pending_emails(cfg["mail"])
        except Exception:
            traceback.print_exc()
    run_id = db.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now_iso(),)).lastrowid
    db.commit()
    stats = {"fetched": 0, "inserted": 0, "classified": 0, "passed": 0, "trends": 0}

    def finish(status: str, error: str | None = None, message_ids: list[str] | None = None) -> None:
        db.execute(
            """UPDATE runs SET finished_at = ?, status = ?, fetched = ?, inserted = ?, classified = ?, passed = ?,
               trends = ?, discord_message_ids = ?, error = ? WHERE id = ?""",
            (now_iso(), status, stats["fetched"], stats["inserted"], stats["classified"], stats["passed"],
             stats["trends"], json.dumps(message_ids) if message_ids else None, error, run_id),
        )
        db.commit()

    stage = "TL取得"
    try:
        if not args.no_fetch:
            rows = asyncio.run(fetch_timeline(cfg["fetch"], args.pages or cfg["fetch"]["pages"]))
            stats["fetched"] = len(rows)
            stats["inserted"] = insert_tweets(db, rows)
            log(f"TL取得: {stats['fetched']}件（新規 {stats['inserted']}件）")

        stage = "Clef判定"
        stats["classified"], _ = classify(db, cfg["clef"])
        log(f"Clef判定: {stats['classified']}件")

        candidates = select_candidates(db, cfg["clef"], cfg["summary"])
        stats["passed"] = len(candidates)
        log(f"判定通過: {stats['passed']}件")
        if args.no_summary:
            finish("classified_only")
            return
        if not candidates:
            log("候補がないので送らない")
            finish("skipped")
            return

        stage = "claude -p 要約"
        trends = summarize(candidates, cfg["summary"])
        stats["trends"] = len(trends)
        log(f"トレンド: {stats['trends']}件")
        if not trends:
            if not args.dry_run:
                db.executemany("UPDATE tweets SET digest_run = ? WHERE id = ?", [(run_id, r["id"]) for r in candidates])
            log("まとめるトレンドがないので送らない")
            finish("skipped")
            return

        stage = "Discord送信"
        header = (
            f"**XおすすめTLの技術トレンド** {datetime.now(JST):%m/%d %H:%M}\n"
            f"取得 {stats['fetched']}件（新規 {stats['inserted']}件）→ 判定通過 {stats['passed']}件 → {stats['trends']}トピック"
        )
        row_map = {r["id"]: r for r in candidates}
        messages = build_messages(header, trends, row_map)
        stats_line = header.split("\n", 1)[1]
        mail_cfg = cfg.get("mail", {})
        email = build_email(trends, row_map, stats_line) if mail_cfg.get("enabled") else None
        if args.dry_run:
            print(json.dumps(messages, ensure_ascii=False, indent=2))
            if email and args.email_preview:
                args.email_preview.write_text(email[2])
                log(f"メールの件名: {email[0]} / HTML: {args.email_preview}")
            finish("dry_run")
            return
        webhook = keychain_get(WEBHOOK_SERVICE)
        message_ids = post_messages(webhook, messages)
        db.executemany("UPDATE tweets SET digest_run = ? WHERE id = ?", [(run_id, r["id"]) for r in candidates])
        finish("posted", message_ids=message_ids)
        log(f"Discord送信: message_id={','.join(message_ids)}")

        if email:
            # Discord は送れているので、メールの失敗は警告に留めて run を posted のままにする。
            stage = "メール送信"
            try:
                result = send_email(mail_cfg, *email)
                log(f"メール送信: {','.join(mail_cfg['to'])} {result}")
            except Exception as e:
                traceback.print_exc()
                saved = save_pending_email(mail_cfg, email)
                log(f"メールを保存した（次回再送）: {saved.name}")
                finish("posted", error=f"{stage}: {e}", message_ids=message_ids)
                try:
                    post_discord(webhook, {
                        "content": f"[x-tl-digest] メール送信失敗: {one_line(str(e), 300)}。pending に保存（{saved.name}）",
                        "allowed_mentions": {"parse": []},
                    })
                except Exception:
                    traceback.print_exc()
    except Exception as e:
        traceback.print_exc()
        finish("failed", error=f"{stage}: {e}")
        if not args.dry_run:
            try:
                post_discord(keychain_get(WEBHOOK_SERVICE), {
                    "content": f"[x-tl-digest] {stage}で失敗した: {one_line(str(e), 300)}",
                    "allowed_mentions": {"parse": []},
                })
            except Exception:
                traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
