# x-tl-digest

English | [日本語](README.ja.md)

x-tl-digest picks up tech trends from your X "For you" timeline and sends summaries, along with the AI's own take, to Discord and as an email newsletter. On macOS, launchd runs it twice a day, at 10:00 and 20:00.

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

1. Fetches 5 pages of the "For you" timeline with [twifork](https://github.com/PawiX25/twifork) (a maintained fork of twikit), using your cookies.
2. Stores the posts in `data/digest.sqlite`. Retweets are collapsed into the original post.
3. Classifies each post with Clef on Cloudflare Workers AI (`@cf/cloudflare/clef`): is it about tech, is it worth the reader's attention, and how important is it.
4. Passes the posts above the thresholds to `claude -p`, which writes a summary and a take for each trend.
5. Sends each trend to Discord with its summary, the AI's take, key posts, and external URLs you can load into NotebookLM. Nothing is sent when there are no candidates.
6. If `[mail]` is enabled, sends the same content as an HTML email.
7. If fetching or classification fails, posts a one-line warning to Discord.

## Caution

The timeline is fetched by calling X's private API with the cookies of a logged-in account. This may violate X's Terms of Service, and the account may be restricted. Use it at your own discretion.

## Requirements

- macOS (secrets live in the Keychain, scheduling uses launchd)
- [uv](https://docs.astral.sh/uv/)
- An X account and its cookies (`auth_token` and `ct0`)
- A Discord webhook URL
- The `cf` CLI, logged in with `cf auth login` to an account that can use Workers AI
- The `claude` CLI (Claude Code), logged in
- For email: [gog](https://gogcli.sh) (gogcli) and a Gmail account

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
| Key for gog's encrypted file keyring (email only) | `x-tl-digest.gog-keyring` |

```sh
security add-generic-password -U -s x-tl-digest.x-cookies -a "$USER" \
  -w '{"auth_token":"...","ct0":"..."}'
security add-generic-password -U -s x-tl-digest.discord-webhook -a "$USER" \
  -w 'https://discord.com/api/webhooks/...'
```

You can find the cookies in DevTools while logged in to x.com. When they expire, run the same command again.

### Sending email

gog's tokens are kept in an encrypted file, and only the key for that file goes in the Keychain. If gog used the Keychain directly, it would stop at a permission dialog when run from launchd.

```sh
security add-generic-password -U -s x-tl-digest.gog-keyring -a "$USER" -w "$(openssl rand -hex 32)"
export GOG_KEYRING_BACKEND=file
export GOG_KEYRING_PASSWORD="$(security find-generic-password -s x-tl-digest.gog-keyring -a "$USER" -w)"
gog auth credentials set client_secret.json   # an OAuth client created in Google Cloud
gog auth add you@example.com --services gmail
```

Then set `enabled = true` under `[mail]` in `config.toml` and fill in `account` and `to`. To send from a different address, add it as a "Send mail as" address in Gmail settings (Accounts and Import) and put it in `from`. If that address's display name contains a comma, gog writes a broken From header and Gmail replaces it with your primary address.

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
uv run digest.py              # same as the scheduled run
uv run digest.py --dry-run    # print what would be sent, without sending
uv run digest.py --dry-run --email-preview mail.html   # also write the email HTML
uv run digest.py --no-fetch   # skip fetching; use only what is already in the DB
```

## Scheduling

`install-launchd.sh` fills in the paths in `launchd/x-tl-digest.plist` and registers it in `~/Library/LaunchAgents`. Run it again whenever you change the plist. The label defaults to `local.x-tl-digest`; set the `LABEL` environment variable to change it.

```sh
./install-launchd.sh
launchctl kickstart gui/$(id -u)/local.x-tl-digest   # run once right now
```

When running under launchd, keep the repository outside `~/Documents`. Inside it, uv started by launchd stops at a dialog asking for access to the Documents folder.

Logs go to `logs/digest.log`. Per-run counts and Discord message IDs are recorded in the `runs` table.

## License

MIT
