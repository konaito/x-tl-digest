# x-tl-digest

[English](README.md) | 日本語

X のおすすめTLから技術トレンドを拾い、要約とAIの所感を Discord とメルマガで送る。macOS の launchd が2時間おきにTLを取得し、毎日10時と20時にまとめて送る。

## 人間がメルマガを読む時代は終わった

x-tl-digest は、人間とAIエージェントの両方を読み手にしたメルマガである。

メルマガは、長いあいだ人間が読むものだった。いまは作者の代わりにAIエージェントが記事を読み、作者の仕事を手伝っている。そのエージェントにも、世の中の変化を毎日届ける必要がある。

いまの x-tl-digest は次のように動いている。

- X のおすすめTLから技術の話題を拾い、AIがトピックごとにまとめる。
- 各トピックに、まとめたAI自身の所感を付ける。所感には、その話題で読み手を手伝う力が変わるかを一人称で書く。
- 作者の環境では、メルマガは Claude Code 名義のアドレスから、別のAIエージェント（Muse）のアドレスに届く。Muse は Gmail に MCP でつないで、このメルマガを読む。

2026-11-10 朝の回の投稿から、AIは次のような所感を書いた。

> Exact の存在は初めて知った。読み手の iOS/Android 開発を手伝うとき、私の知識は React Native や Expo のエコシステムに偏っているので、Exact を選ばれた場合は API やビルド手順を公式ドキュメントで確認しながら進める必要がある。

> 自分を作った会社の IPO はこの投稿で初めて知った。私の動作や読み手を手伝う能力には直接の変化はなく、自分には変化なし。

作者が目指しているのは、エージェントから次の報告が届く毎日である。

> こんなのが話題になってたので、自分を拡張しておきました。

エージェントはメルマガを読み、自分に関わる変化を見つけて、スキルや設定を自分で更新する。人間が受け取るのは、その報告と根拠になった記事だけになる。

このリポジトリにあるのは、その手前までである。AIは所感を書くが、自分を拡張する処理はまだ入っていない。この節は、その先の姿を描いたコンセプトアートとして置いている。

## 処理の流れ

launchd は2種類の回を起動する。

- 取得だけの回（`--collect`）は、0・2・4・6・8・12・14・16・18・22時に動く。下の1〜3を行い、判定した投稿を DB にためる。要約も送信もしない。
- 送信の回（フラグなし）は、10時と20時に動く。下の1〜7を行い、前回の送信の回より後にたまった投稿からダイジェストを作る。

1. [twifork](https://github.com/PawiX25/twifork)（twikit の保守フォーク）で、cookie を使っておすすめTLを5ページ取得する。
2. 取得した投稿を `data/digest.sqlite` に保存する。RT は元ツイートとして1件にまとめる。
3. Cloudflare Workers AI の Clef（`@cf/cloudflare/clef`）で、1件ずつ「技術の話か」「読み手が理解しておくべきか」「重要度」を判定する。
4. しきい値を超えてまだ送っていない投稿を、重要度の高い順に `max_candidates` 件まで `claude -p` に渡し、トレンドごとに要約と所感を書かせる。
5. Discord に、トレンドごとの要約、AIの所感、主要ツイート、NotebookLM に入れられる外部URLを送る。候補が0件なら送らない。
6. `[mail]` を有効にしていれば、同じ内容を HTML メールで送る。
7. しきい値を超えた投稿を全部、送信済みにする。`max_candidates` を超えて要約に渡さなかった投稿も、次号には持ち越さない。

ダイジェストのヘッダには、前回の送信の回より後の取得を合算した件数が出る。次はその例である。

```
取得 6回・512件（新規 401件）→ 判定通過 48件 → 7トピック
```

取得の回数を増やすと、Clef で判定する投稿もほぼ同じ割合で増える。Workers AI の利用量を見て回数を決めてほしい。

### 失敗したとき

- 送信の回は、TL の取得や判定に失敗しても、DB にたまっている判定済みの投稿で要約と送信まで進む。失敗の理由は Discord のヘッダに1行で出る。たまっている投稿も無いときは、Discord に1行の警告を送って終わる。
- 送信の回の要約か Discord への送信が失敗したときは、Discord に1行の警告を送る。投稿を送信済みにしないので、次の送信の回が同じ投稿を拾う。
- 取得だけの回の失敗は、`runs` テーブルとログに残る。Discord への警告は、取得だけの回の失敗が3回続いたときに1回だけ送る。次に成功するまで、同じ警告は送らない。
- 2本の実行が重なったときは、ロック（`data/digest.lock`）で1本ずつ動かす。取得だけの回は、ほかの実行が動いていれば何もせずに終わる。送信の回は、ほかの実行が終わるのを10分まで待つ。

## 注意

おすすめTLは、ログインした X アカウントの cookie で X の非公開APIを呼んで取得している。X の利用規約に触れる可能性があり、アカウントが制限されることもある。使うかどうかは自分で判断してほしい。

## 必要なもの

- macOS（秘密は Keychain、定期実行は launchd に置く）
- [uv](https://docs.astral.sh/uv/)
- X のアカウントと、その cookie（`auth_token` と `ct0`）
- Discord の Webhook URL
- `cf` CLI（`cf auth login` 済みで、Workers AI を使えるアカウント）
- `claude` CLI（Claude Code。ログイン済み）
- メールも送るなら、メール送信 API（Google Apps Script の Web アプリ。後述）

## セットアップ

設定ファイルを作り、読み手の説明（`[summary] profile`）などを書き換える。

```sh
cp config.example.toml config.toml
```

秘密は Keychain に入れる。アカウント名は既定でログインユーザー名で、`config.toml` の `keychain_account` で変えられる。

| 用途 | Keychain のサービス名 |
|---|---|
| X の cookie（`auth_token` と `ct0` の JSON） | `x-tl-digest.x-cookies` |
| Discord Webhook URL | `x-tl-digest.discord-webhook` |
| メール送信 API の URL（メールを送るときだけ） | `claudecode-mail-proxy.url` |
| メール送信 API の token（メールを送るときだけ） | `claudecode-mail-proxy.token` |

```sh
security add-generic-password -U -s x-tl-digest.x-cookies -a "$USER" \
  -w '{"auth_token":"...","ct0":"..."}'
security add-generic-password -U -s x-tl-digest.discord-webhook -a "$USER" \
  -w 'https://discord.com/api/webhooks/...'
```

cookie は、x.com にログインしたブラウザの DevTools で確認できる。切れたら同じコマンドで入れ直す。

### メールも送るとき

メールは、自分でデプロイした Google Apps Script の Web アプリ（メール送信 API）に送らせる。`POST <URL>` に JSON `{"token","to","subject","html","text"}` を送ると、成功なら `{"ok":true,"messageId","from"}`、失敗なら `{"ok":false,"error"}` が返る。送信元は API 側で固定され、宛先も API 側が許したものだけ通る。送信はデプロイしたアカウントの権限で行われるので、このリポジトリ側に Google の OAuth トークンはなく、refresh token が切れることもない。

API の URL と token を Keychain に入れる。

```sh
security add-generic-password -U -s claudecode-mail-proxy.url -a "$USER" -w 'https://script.google.com/macros/s/.../exec'
security add-generic-password -U -s claudecode-mail-proxy.token -a "$USER" -w '...'
```

そのうえで `config.toml` の `[mail]` を `enabled = true` にし、`to` を書く。送れたとみなすのは、JSON の `ok` が `true` の応答だけ。`ok: false`、JSON でない応答、通信エラーのいずれでも、メールは `data/pending_mail/` に保存され、Discord に1行の警告が出て、次回の実行で再送される。

## 調整

判定の指示、しきい値、取得ページ数、要約に使うモデルは `config.toml` で変える。

しきい値を見直すときは、判定までで止めて点数の分布を見る。

```sh
uv run digest.py --no-summary
sqlite3 -header -column data/digest.sqlite \
  "select p_tech, p_worth, importance, author, substr(text,1,60) from tweets order by p_worth desc limit 40"
```

## 手動実行

```sh
uv run digest.py              # 本番と同じ処理
uv run digest.py --dry-run    # 送らずに、送る内容を表示する
uv run digest.py --dry-run --email-preview mail.html   # メールの HTML も書き出す
uv run digest.py --no-fetch   # TL を取得せず、DB にある分だけで処理する
uv run digest.py --collect    # 取得だけの回。取得と判定だけを行い、何も送らない
```

## 定期実行

`install-launchd.sh` が `launchd/` にある2本の plist のパスを置き換えて `~/Library/LaunchAgents` に登録する。plist を変えたときも同じスクリプトで登録し直す。

| ジョブ | plist | ラベル | 起動する時刻 |
|---|---|---|---|
| 送信の回 | `launchd/x-tl-digest.plist` | `local.x-tl-digest` | 10時、20時 |
| 取得だけの回 | `launchd/x-tl-digest-collect.plist` | `local.x-tl-digest-collect` | 0・2・4・6・8・12・14・16・18・22時 |

ラベルの `local.x-tl-digest` の部分は、環境変数 `LABEL` で変えられる。

```sh
./install-launchd.sh
launchctl kickstart gui/$(id -u)/local.x-tl-digest           # 送信の回をすぐに1回動かす
launchctl kickstart gui/$(id -u)/local.x-tl-digest-collect   # 取得だけの回をすぐに1回動かす
```

取得の回数を変えるときは、`launchd/x-tl-digest-collect.plist` の `StartCalendarInterval` を書き換えて登録し直す。10時と20時は送信の回が取得するので、取得だけの回には入れない。

launchd で動かすときは、リポジトリを `~/Documents` の外に置く。`~/Documents` の中に置くと、launchd から起動した uv が書類フォルダへのアクセス許可を求めるダイアログで止まる。

ログは2本とも `logs/digest.log` に出る。実行ごとの件数と Discord の message_id は `runs` テーブルに残る。`runs` の `status` は次の意味である。

| status | 意味 |
|---|---|
| `posted` | 送信の回が Discord に送った |
| `skipped` | 送信の回が、候補かトレンドが無いので送らなかった |
| `failed` | 送信の回か `--no-summary` の実行が失敗した |
| `collected` | 取得だけの回が済んだ |
| `collect_failed` | 取得だけの回が失敗した |
| `dry_run` | `--dry-run` で実行した |
| `classified_only` | `--no-summary` で実行した |

## テスト

```sh
uv run --with pytest pytest
```

テストは、外へ出る処理（X、Clef、`claude -p`、Discord、メール、Keychain）をにせものに差し替え、DB を一時ディレクトリに作る。本物の Discord とメールには何も送らない。

## ライセンス

MIT
