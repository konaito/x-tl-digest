# x-tl-digest

English | [日本語](README.ja.md)

x-tl-digest picks up tech trends from your X "For you" timeline and sends summaries, along with the AI's own take, to Discord and as an email newsletter. On macOS, launchd fetches the timeline every two hours and sends a digest twice a day, at 10:00 and 20:00.

## The era of humans reading newsletters is over

x-tl-digest is a newsletter written for two kinds of readers: humans and AI agents.

For a long time, newsletters were something people read. Today, AI agents read articles on the author's behalf and help with the author's work. Those agents also need a daily feed of what is changing in the world.

Here is what x-tl-digest does today.

- It picks up tech topics from the X timeline, and an AI groups them into trends.
- Each trend comes with the AI's own take, written in the first person: does this news change how well it can help the reader?
- In the author's setup, the newsletter is sent from a Claude Code address to the address of another AI agent, Muse. Muse reads it through Gmail over MCP.

From the posts in the morning run of 2026-11-10, the AI wrote takes like these (translated from Japanese).

> I hadn't heard of Exact before. When I help with the reader's iOS/Android work, my knowledge leans toward React Native and Expo, so if they choose Exact I'll need to check the API and build steps against the official docs as I go.

> This post is the first I've heard of the IPO of the company that made me. It doesn't directly change how I work or how well I can help the reader. No change for me.

What the author is aiming for is a daily message like this from an agent:

> This was trending, so I went ahead and extended myself.

The agent reads the newsletter, finds the changes that matter to it, and updates its own skills and settings. What reaches the human is that report, plus the articles behind it.

This repository stops short of that. The AI writes its take, but nothing here extends the agent yet. This section is concept art for where it is headed.

## How it works

launchd starts two kinds of runs.

- A collect run (`--collect`) starts at 0:00, 2:00, 4:00, 6:00, 8:00, 12:00, 14:00, 16:00, 18:00, and 22:00. It does steps 1 to 3 below and keeps the classified posts in the database. It does not summarize or send anything.
- A send run (no flag) starts at 10:00 and 20:00. It does steps 1 to 7 and builds the digest from the posts collected since the previous send run.

1. Fetches 5 pages of the "For you" timeline with [twifork](https://github.com/PawiX25/twifork) (a maintained fork of twikit), using your cookies.
2. Stores the posts in `data/digest.sqlite`. Retweets are collapsed into the original post.
3. Classifies each post with Clef on Cloudflare Workers AI (`@cf/cloudflare/clef`): is it about tech, is it worth the reader's attention, and how important is it.
4. Passes the posts that are above the thresholds and not sent yet to `claude -p`, most important first, up to `max_candidates`. `claude -p` writes a summary and a take for each trend.
5. Sends each trend to Discord with its summary, the AI's take, key posts, and external URLs you can load into NotebookLM. Nothing is sent when there are no candidates.
6. If `[mail]` is enabled, sends the same content as an HTML email.
7. Marks every post above the thresholds as sent. Posts beyond `max_candidates`, which were not passed to the summary, are not carried over to the next digest either.

The digest header shows the totals of all fetches since the previous send run. For example (the header itself is in Japanese):

```
取得 6回・512件（新規 401件）→ 判定通過 48件 → 7トピック
```

This reads: 6 fetches, 512 posts (401 new), 48 above the thresholds, 7 topics.

More fetches mean more posts for Clef to classify, at roughly the same rate. Choose the number of collect runs with your Workers AI usage in mind.

### When something fails

- A send run keeps going when fetching or classification fails. It summarizes and sends the classified posts already in the database, and the reason for the failure appears as one line in the Discord header. If the database has nothing to send either, it posts a one-line warning to Discord and stops.
- When the summary or the Discord post fails in a send run, it posts a one-line warning to Discord. The posts are not marked as sent, so the next send run picks them up again.
- A failed collect run is recorded in the `runs` table and the log. A warning goes to Discord only once, when collect runs have failed three times in a row. It is not repeated until a collect run succeeds again.
- When two runs overlap, a lock (`data/digest.lock`) makes them run one at a time. A collect run exits without doing anything if another run is in progress. A send run waits up to 10 minutes for the other run to finish.

## Caution

The timeline is fetched by calling X's private API with the cookies of a logged-in account. This may violate X's Terms of Service, and the account may be restricted. Use it at your own discretion.

## Requirements

- macOS (secrets live in the Keychain, scheduling uses launchd)
- [uv](https://docs.astral.sh/uv/)
- An X account and its cookies (`auth_token` and `ct0`)
- A Discord webhook URL
- The `cf` CLI, logged in with `cf auth login` to an account that can use Workers AI
- The `claude` CLI (Claude Code), logged in
- For email: a mail-sending API (a Google Apps Script web app, see below)

The prompts, the summaries, and the takes are in Japanese. To change the language, edit `SUMMARY_PROMPT` in `digest.py`.

## Setup

Create your config, then edit the reader description (`[summary] profile`) and anything else you need.

```sh
cp config.example.toml config.toml
```

Secrets go in the Keychain. The account name defaults to your login user name; set `keychain_account` in `config.toml` to change it.

| Purpose | Keychain service name |
|---|---|
| X cookies (JSON with `auth_token` and `ct0`) | `x-tl-digest.x-cookies` |
| Discord webhook URL | `x-tl-digest.discord-webhook` |
| Mail-sending API URL (email only) | `claudecode-mail-proxy.url` |
| Mail-sending API token (email only) | `claudecode-mail-proxy.token` |

```sh
security add-generic-password -U -s x-tl-digest.x-cookies -a "$USER" \
  -w '{"auth_token":"...","ct0":"..."}'
security add-generic-password -U -s x-tl-digest.discord-webhook -a "$USER" \
  -w 'https://discord.com/api/webhooks/...'
```

You can find the cookies in DevTools while logged in to x.com. When they expire, run the same command again.

### Sending email

Email goes through a small mail-sending API: a Google Apps Script web app that you deploy yourself. It accepts `POST <URL>` with a JSON body `{"token","to","subject","html","text"}` and answers `{"ok":true,"messageId","from"}` or `{"ok":false,"error"}`. The sender address is fixed on the API side, and the API only accepts the recipients you allow. Because the web app sends as the account that deployed it, nothing in this repo holds a Google OAuth token, so there is no refresh token to expire.

Put the API's URL and token in the Keychain:

```sh
security add-generic-password -U -s claudecode-mail-proxy.url -a "$USER" -w 'https://script.google.com/macros/s/.../exec'
security add-generic-password -U -s claudecode-mail-proxy.token -a "$USER" -w '...'
```

Then set `enabled = true` and `to` under `[mail]` in `config.toml`. Only an `ok: true` JSON response counts as sent. If sending fails (an `ok: false`, a non-JSON response, or a network error), the email is saved in `data/pending_mail/`, one line is posted to Discord, and the next run sends it again.

## Tuning

The classification instructions, thresholds, number of pages, and summary model are all in `config.toml`.

To revisit the thresholds, stop after classification and look at the score distribution.

```sh
uv run digest.py --no-summary
sqlite3 -header -column data/digest.sqlite \
  "select p_tech, p_worth, importance, author, substr(text,1,60) from tweets order by p_worth desc limit 40"
```

## Running manually

```sh
uv run digest.py              # same as a scheduled send run
uv run digest.py --dry-run    # print what would be sent, without sending
uv run digest.py --dry-run --email-preview mail.html   # also write the email HTML
uv run digest.py --no-fetch   # skip fetching; use only what is already in the DB
uv run digest.py --collect    # collect run: fetch and classify only, send nothing
```

## Scheduling

`install-launchd.sh` fills in the paths in the two plists under `launchd/` and registers them in `~/Library/LaunchAgents`. Run it again whenever you change a plist.

| Job | plist | Label | Starts at |
|---|---|---|---|
| Send run | `launchd/x-tl-digest.plist` | `local.x-tl-digest` | 10:00, 20:00 |
| Collect run | `launchd/x-tl-digest-collect.plist` | `local.x-tl-digest-collect` | 0:00, 2:00, 4:00, 6:00, 8:00, 12:00, 14:00, 16:00, 18:00, 22:00 |

Set the `LABEL` environment variable to change the `local.x-tl-digest` part of the labels.

```sh
./install-launchd.sh
launchctl kickstart gui/$(id -u)/local.x-tl-digest           # start a send run right now
launchctl kickstart gui/$(id -u)/local.x-tl-digest-collect   # start a collect run right now
```

To change how often the timeline is fetched, edit `StartCalendarInterval` in `launchd/x-tl-digest-collect.plist` and register it again. Leave 10:00 and 20:00 out of the collect job, because the send run fetches at those times.

When running under launchd, keep the repository outside `~/Documents`. Inside it, uv started by launchd stops at a dialog asking for access to the Documents folder.

Both jobs log to `logs/digest.log`. Per-run counts and Discord message IDs are recorded in the `runs` table. The `status` column of `runs` means the following.

| status | Meaning |
|---|---|
| `posted` | A send run posted to Discord |
| `skipped` | A send run had no candidates or no trends, so it sent nothing |
| `failed` | A send run or a `--no-summary` run failed |
| `collected` | A collect run finished |
| `collect_failed` | A collect run failed |
| `dry_run` | Run with `--dry-run` |
| `classified_only` | Run with `--no-summary` |

## Tests

```sh
uv run --with pytest pytest
```

The tests replace everything that reaches outside (X, Clef, `claude -p`, Discord, email, the Keychain) with fakes and create the database in a temporary directory. Nothing is sent to the real Discord or by email.

## License

MIT
