# oh-bot

A bot that watches CMU's Office Hours Queue (OHQ) for a course and posts a Discord
notification whenever a new student joins the queue.

## Setup

1. Install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install python-socketio requests python-dotenv playwright
   playwright install chromium
   ```

2. Create a `.env` file in the project root with your Discord webhook URL:

   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```

3. Log in to OHQ to generate a session cookie:

   ```bash
   python refresh_cookie.py
   ```

   This opens a Chromium window. Log in with your Andrew ID and Duo, wait until
   you see the queue page, then press Enter in the terminal. This saves a
   `cookie.txt` file used for authentication.

## Running

```bash
source venv/bin/activate
python oh_bot.py
```

The bot connects to the OHQ Socket.IO server and posts to Discord when someone
joins the queue. If the session cookie expires, it will automatically prompt
you to log in again via `refresh_cookie.py`.

By default the bot watches the course with ID `12`. Change `COURSE_ID` in
`oh_bot.py` to watch a different course.
