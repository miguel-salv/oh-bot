import os
import time
import logging
import subprocess
import socketio
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)

seen_ids = set()
COURSE_ID = "12"

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# Set by connect_error when the server rejects our auth, read by the main loop
auth_failed = False


def load_session_cookie():
    try:
        with open("cookie.txt") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def refresh_session_cookie():
    """Blocks until the user finishes logging in via the browser popped by refresh_cookie.py."""
    print("\n=== Session expired. Launching browser for re-login. ===")
    print("A Chromium window will open. Log in with Andrew ID + Duo,")
    print("then press Enter in THAT script's terminal prompt once you see the queue page.\n")
    subprocess.run(["python", "refresh_cookie.py"], check=True)
    print("=== Cookie refreshed, reconnecting... ===\n")


def notify_discord(entry):
    name = f'{entry["first_name"]} {entry["last_name"]}'.strip()
    msg = f'🎓 **{name}** joined the queue — *{entry["topic"]}* ({entry["location"]})'
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})
    if resp.status_code >= 300:
        print(f"Discord post failed: {resp.status_code} {resp.text}")


def handle_questions(data):
    global seen_ids
    entries = data.get("payload", [])
    current_ids = {e["id"] for e in entries if e["state"] == "on_queue"}
    new_ids = current_ids - seen_ids

    for entry in entries:
        if entry["id"] in new_ids:
            notify_discord(entry)

    seen_ids = current_ids


def build_client():
    """Creates a fresh Socket.IO client with all handlers attached.
    Rebuilt on each reconnect attempt rather than reusing one client instance."""
    sio = socketio.Client(logger=False, engineio_logger=False)

    @sio.on("questions_initial", namespace="/queue")
    def on_questions_initial(data):
        handle_questions(data)

    @sio.on("questions", namespace="/queue")
    def on_questions(data):
        handle_questions(data)

    @sio.event(namespace="/queue")
    def connect():
        print("Connected to /queue namespace, joining course...")
        sio.emit("join_course", COURSE_ID, namespace="/queue")

    @sio.event(namespace="/queue")
    def connect_error(data):
        global auth_failed
        print(f"Connection to /queue rejected: {data}")
        if data == "Not authorized":
            auth_failed = True

    @sio.event
    def disconnect():
        print("Disconnected from server.")

    return sio


def run_forever():
    global auth_failed

    while True:
        auth_failed = False
        session_cookie = load_session_cookie()

        if session_cookie is None:
            print("No cookie.txt found — need to log in first.")
            refresh_session_cookie()
            continue

        sio = build_client()

        try:
            sio.connect(
                "https://ohq.eberly.cmu.edu",
                namespaces=["/queue"],
                headers={"Cookie": session_cookie},
                transports=["polling"],
            )
        except Exception as e:
            print(f"Connection attempt failed: {e}")
            time.sleep(5)
            continue

        # Poll instead of sio.wait(), so we can notice an auth failure
        # flagged by connect_error and break out to trigger a refresh.
        while True:
            time.sleep(2)
            if auth_failed:
                sio.disconnect()
                break
            if not sio.connected:
                break

        if auth_failed:
            refresh_session_cookie()
        else:
            print("Connection lost, retrying in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    run_forever()