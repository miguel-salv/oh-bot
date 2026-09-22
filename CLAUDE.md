# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small bot that watches CMU's Office Hours Queue (OHQ, at `ohq.eberly.cmu.edu`) for a specific course via its Socket.IO backend, and posts a Discord notification whenever a new student joins the queue.

## Running

```bash
source venv/bin/activate
python oh_bot.py
```

Requires a `.env` file with `DISCORD_WEBHOOK_URL` set, and a valid `cookie.txt` (see below). There is no test suite, linter, or build step configured.

To manually re-authenticate and refresh the session cookie:

```bash
python refresh_cookie.py
```

This launches a visible (non-headless) Chromium window via Playwright, waits for the user to log in with Andrew ID + Duo, then saves the resulting `session_id` cookie to `cookie.txt`.

## Architecture

- **`oh_bot.py`** — the bot's main loop (`run_forever`). It builds a fresh `socketio.Client` per connection attempt (`build_client`), connects to the `/queue` namespace using the cookie in `cookie.txt` as the auth header, and joins `COURSE_ID` (currently hardcoded to `"12"`). It listens for `questions_initial`/`questions` events, diffs the queue entries against an in-memory `seen_ids` set to detect newly-`on_queue` students, and posts one Discord message per new entry via `notify_discord`.
- **Auth failure recovery**: the OHQ session cookie expires periodically. When the socket rejects auth (`connect_error` firing with `"Not authorized"`), the module-level `auth_failed` flag is set. The main loop polls this flag (rather than blocking on `sio.wait()`) so it can notice the failure, disconnect, and call `refresh_session_cookie()`, which shells out to `refresh_cookie.py` and blocks until the user completes the interactive login. The loop then reconnects with the fresh cookie. This poll-based design (instead of an async/event-driven wait) is intentional — it's what lets the main thread interrupt and trigger the browser-based reauth flow.
- **`refresh_cookie.py`** is a standalone script, invoked as a subprocess by `oh_bot.py`, not imported. It always runs with a visible browser window since Duo 2FA requires human interaction.
- **`cookie.txt`** and **`.env`** are gitignored — they hold the live session cookie and the Discord webhook URL respectively, and are the only required local setup besides `pip install`.
- **`test.py`** is a scratch script for manually checking that `cookie.txt` still authenticates (`GET /user`). It reads the cookie from `cookie.txt` like `oh_bot.py` does — never hardcode a session cookie in this file.
