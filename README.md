# oh-bot

A bot that watches CMU's Office Hours Queue (OHQ) for a course and posts a Discord
notification whenever a new student joins the queue.

## Setup

1. Install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

   OHQ speaks Engine.IO 3. `requirements.txt` keeps `python-socketio` on 4.x,
   which is the client that can complete that handshake.

2. Create a `.env` file in the project root:

   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
   REAUTH_PASSWORD=choose-a-password
   REAUTH_BASE_URL=http://100.x.x.x:8787
   QUEUE_AUTO_OPEN=true
   QUEUE_OPEN_DAYS=Sun,Mon,Tue,Wed,Thu
   QUEUE_OPEN_TIME=17:00
   QUEUE_CLOSE_TIME=20:00
   ```

   `DISCORD_WEBHOOK_URL` is the shared channel for queue joins.
   `DISCORD_ALERT_WEBHOOK_URL` is a webhook in a channel only you can see.
   Webhooks cannot send DMs, so that private channel is where session alerts go.
   `REAUTH_PASSWORD` unlocks the login page.
   `REAUTH_BASE_URL` is the address your phone uses to reach the machine running
   the bot, including the port. Use Tailscale or another private network. Do not
   port-forward this page to the public internet.
   `QUEUE_AUTO_OPEN` turns automatic open and close on or off. Set it to `false`
   to leave the queue alone. `QUEUE_OPEN_DAYS`, `QUEUE_OPEN_TIME`, and
   `QUEUE_CLOSE_TIME` are the window, in 24-hour Eastern time. The close time
   has to be later on the same day. The bot re-reads these when `.env` changes,
   so you do not have to restart it. Leave them out to keep Sunday–Thursday,
   5:00pm–8:00pm. The 2026–27 CMU calendar is built in: the queue stays closed
   outside fall and spring, including finals' surrounding breaks, and on Labor
   Day, fall break, Thanksgiving, spring break, and Spring Carnival. Democracy
   Day is not skipped, because evening classes after 5:00pm still meet. Add
   more days with `QUEUE_SKIP_DATES=2026-10-05`.

3. Log in once so later refreshes can reuse the browser profile:

   ```bash
   python refresh_cookie.py
   ```

   Chromium stays headless and stores its profile in `browser_profile/`. If that
   profile is already logged in, this writes `cookie.txt` and exits. Otherwise it
   posts a one-time link to the private Discord channel. Open the link, enter
   `REAUTH_PASSWORD`, then finish Andrew ID and Duo on that page. The link expires
   in 15 minutes and stops working after a successful login.

## Running

```bash
source venv/bin/activate
python oh_bot.py
```

On the Pi, leave it running in the background instead:

```bash
./bot.sh start
./bot.sh log
./bot.sh stop
```

`start` does nothing if `oh_bot.py` is already running, including after the
reboot cron job. `log` follows `oh_bot.log`, and `Ctrl+C` only stops the
viewer.

The bot connects to the OHQ Socket.IO server and posts to Discord when someone
joins the queue. If the session cookie expires, it opens the saved browser
profile and writes a new `cookie.txt` when CMU still accepts that login. When a
real login is required, it sends a one-time link to the private channel. If that
link expires, it sends a new one 30 minutes later. While automatic open is
enabled, it opens course 12 on the configured Eastern
schedule. After the close time it waits until nobody is left on the queue, then
closes it. The private channel is told only after OHQ confirms the change.
Starting the bot during that window opens the queue immediately. The account
in the saved browser profile is the one OHQ sees.
Turning `QUEUE_AUTO_OPEN` off stops the bot from opening or closing the queue.
`browser_profile/` holds the Andrew session and the Duo remember-this-browser
cookie. Do not commit it.

By default the bot watches the course with ID `12`. Change `COURSE_ID` in
`oh_bot.py` to watch a different course.
