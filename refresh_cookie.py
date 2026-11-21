import hashlib
import hmac
import os
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

OHQ_URL = "https://ohq.eberly.cmu.edu"
PROFILE_DIR = "browser_profile"
COOKIE_FILE = "cookie.txt"
VIEWPORT = {"width": 420, "height": 780}
DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
LINK_TTL_SECONDS = 15 * 60
SILENT_WAIT_SECONDS = 15
# Same cookie the socket just rejected is not a successful refresh.
PREVIOUS_COOKIE = None
KEYRING_SERVICE = "oh-bot-andrew"
LOGIN_ATTEMPT_FILE = ".andrew-login-attempted"

EXIT_SILENT = 0
EXIT_ERROR = 1
EXIT_INTERACTIVE = 2
EXIT_EXPIRED = 3

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Filled in only while the one-time login page is running.
STATE = {
    "token": "",
    "expires": 0,
    "password": "",
    "session": "",
    "failures": 0,
    "done": False,
    "closed": False,
    "ws_port": 0,
}
SERVER = None
DESKTOP_PROCS = []

VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>OHQ login</title>
</head>
<body>
<p>Opening the Pi's browser…</p>
<script>
const parts = location.pathname.split("/").filter(Boolean);
const token = parts[1];
const path = "r/" + token + "/websockify";
location.replace(
  "novnc/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=" + encodeURIComponent(path)
);
</script>
</body>
</html>
"""


def passwords_match(given, expected):
    return hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def post_alert(text):
    print(text)
    url = os.environ.get("DISCORD_ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        resp = requests.post(url, json={"content": text}, timeout=15)
    except requests.RequestException as e:
        print(f"Alert post failed: {e}")
        return
    if resp.status_code >= 300:
        print(f"Alert post failed: {resp.status_code} {resp.text}")


def load_previous_cookie():
    try:
        with open(COOKIE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def save_cookie(header):
    with open(COOKIE_FILE, "w") as f:
        f.write(header)
    clear_login_attempt()
    print("Saved fresh cookie to cookie.txt")


def clear_login_attempt():
    try:
        os.remove(LOGIN_ATTEMPT_FILE)
    except FileNotFoundError:
        pass


def open_cmu_login(page):
    """Follow OHQ's login link when automatic login is configured."""
    if not os.environ.get("ANDREW_ID", "").strip() or os.path.exists(LOGIN_ATTEMPT_FILE):
        return False
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname != "ohq.eberly.cmu.edu" or parsed.port not in (None, 443):
        return False
    try:
        link = page.locator('a[href="/api/login/cmushib"]').first
        if not link.is_visible(timeout=5000):
            return False
        link.click(timeout=15000)
        print("Opened CMU login from OHQ.", flush=True)
        return True
    except Exception as e:
        print(f"Could not open CMU login ({type(e).__name__}); use the login link.", flush=True)
        return False


def try_saved_credentials(page):
    """Submit at most once until a successful login or credential reset.

    Only the CMU-hosted single-page password form is supported. Unknown login
    screens and Duo are left to the interactive flow.
    """
    username = os.environ.get("ANDREW_ID", "").strip()
    if not username or os.path.exists(LOGIN_ATTEMPT_FILE):
        return False
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname != "login.cmu.edu" or parsed.port not in (None, 443):
        return False
    try:
        import keyring
        password = keyring.get_password(KEYRING_SERVICE, username)
        if not password:
            print("No Andrew password found in the credential store.", flush=True)
            return False
        password_field = page.locator('input[type="password"]:visible').first
        if password_field.count() != 1:
            return False
        form = password_field.locator("xpath=ancestor::form[1]")
        if form.count() != 1:
            return False
        action = form.get_attribute("action") or page.url
        target = urlparse(urljoin(page.url, action))
        if target.scheme != "https" or target.hostname != "login.cmu.edu" or target.port not in (None, 443):
            return False
        user_field = form.locator(
            'input[name="j_username"]:visible, input[autocomplete="username"]:visible, '
            'input[name="username"]:visible, input[id="username"]:visible'
        ).first
        if user_field.count() != 1:
            return False
        # Claim the attempt before filling, so failures cannot repeatedly lock the account.
        fd = os.open(LOGIN_ATTEMPT_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        user_field.fill(username)
        password_field.fill(password)
        password_field.press("Enter")
        print("Submitted saved Andrew credentials; waiting for CMU login.", flush=True)
        return True
    except FileExistsError:
        return False
    except Exception as e:
        # Do not log browser or keyring exceptions, which may contain sensitive data.
        print(f"Automatic login unavailable ({type(e).__name__}); use the login link.", flush=True)
        return False


def on_ohq_url(url):
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "ohq.eberly.cmu.edu"


def on_ohq(page):
    return on_ohq_url(page.url)


def page_shows_ohq(page):
    try:
        if on_ohq(page):
            return True
        for frame in page.frames:
            if on_ohq_url(frame.url):
                return True
    except Exception:
        return False
    return False


def brief_url(url):
    parsed = urlparse(url)
    if not parsed.hostname:
        return url.split("?", 1)[0][:120]
    return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"


def session_header(context):
    for cookie in context.cookies():
        if cookie["name"] == "session_id" and cookie["value"]:
            return f"session_id={cookie['value']}"
    return None


def live_session_header(page):
    """Return the OHQ session header when /api/user accepts it and the page is OHQ."""
    if not page_shows_ohq(page):
        return None
    header = session_header(page.context)
    if not header:
        return None
    try:
        resp = page.request.get(
            OHQ_URL + "/api/user",
            max_redirects=0,
            timeout=5000,
        )
    except Exception as e:
        print(f"Session check failed: {e}")
        return None
    if resp.status != 200:
        return None
    return header


def wait_for_new_session(page, seconds):
    """Return a live session that is not the cookie the socket just rejected.

    A still-valid rejected cookie means the profile should drop session_id and
    load OHQ again, so this returns immediately in that case.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        header = live_session_header(page)
        if header and header != PREVIOUS_COOKIE:
            return header
        if header and PREVIOUS_COOKIE and header == PREVIOUS_COOKIE:
            return None
        page.wait_for_timeout(1000)
    return None


def clear_session_cookie(context):
    context.clear_cookies(name="session_id")


def open_browser(playwright, headless=True, display=None):
    viewport = VIEWPORT if headless else DESKTOP_VIEWPORT
    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    context = playwright.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        viewport=viewport,
        user_agent=CHROME_UA,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled", "--disable-gpu"],
        env=env,
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size(viewport)
    return context, page


def token_ok(token):
    if not token or not STATE["token"] or STATE["closed"]:
        return False
    if time.time() >= STATE["expires"] or STATE["failures"] >= 8:
        return False
    return hmac.compare_digest(
        hashlib.sha256(token.encode()).digest(),
        hashlib.sha256(STATE["token"].encode()).digest(),
    )


def parse_route(path):
    """Return (token, action, rest) for /r/<token>/ and nested paths under it."""
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[0] != "r":
        return None, None, None
    token = parts[1]
    action = parts[2] if len(parts) > 2 else ""
    rest = "/".join(parts[3:])
    return token, action, rest


def request_session(handler):
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "reauth_session":
            return value
    return ""


def unlocked(handler, token):
    session = request_session(handler)
    return bool(session) and hmac.compare_digest(
        hashlib.sha256(session.encode()).digest(),
        hashlib.sha256(STATE["session"].encode()).digest(),
    ) and token_ok(token)


def send_html(handler, status, html):
    body = html.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    handler.wfile.write(body)


def send_bytes(handler, status, content_type, payload, extra_headers=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    if extra_headers:
        for key, value in extra_headers:
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(payload)


def password_form(message):
    note = f"<p>{message}</p>" if message else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>OHQ login</title>
<style>
  body {{ margin: 0; font-family: sans-serif; background: #111; color: #eee; }}
  form {{ padding: 24px; display: flex; flex-direction: column; gap: 12px; max-width: 360px; }}
  input {{ font-size: 16px; padding: 10px; }}
  button {{ font-size: 16px; padding: 10px 12px; }}
</style>
</head>
<body>
<form method="POST" action="unlock">
  <label>Reauth password</label>
  {note}
  <input type="password" name="password" autocomplete="current-password" autofocus>
  <button type="submit">Continue</button>
</form>
</body>
</html>
"""


def read_body(handler, limit=10_000):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length < 0 or length > limit:
        return None
    return handler.rfile.read(length)


NOVNC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


def command_argv(name):
    found = shutil.which(name)
    if found:
        return [found]
    if name in {"x11vnc", "websockify"} and shutil.which("docker"):
        return ["docker", "exec", "oh-bot-login-desktop", name]
    raise RuntimeError(
        f"{name} is not installed. Install the xvfb, x11vnc, novnc, and websockify packages."
    )


def novnc_root():
    candidates = (
        "/usr/share/novnc",
        "/usr/share/novnc/www",
        os.path.expanduser("~/.local/share/novnc"),
    )
    for path in candidates:
        if os.path.isfile(os.path.join(path, "vnc.html")):
            return path
    raise RuntimeError("noVNC is not installed. Install the novnc package.")


def novnc_file(rel):
    root = os.path.realpath(novnc_root())
    if not rel:
        rel = "vnc.html"
    path = os.path.realpath(os.path.join(root, rel))
    if path != root and not path.startswith(root + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    return path


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def wait_until(ready, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ready():
            return
        time.sleep(0.1)
    raise RuntimeError(f"{what} did not become ready")


def spawn(args):
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    DESKTOP_PROCS.append(proc)
    return proc


def start_desktop():
    """Start a private display and a localhost VNC websocket for the headed browser."""
    command_argv("Xvfb")
    command_argv("x11vnc")
    command_argv("websockify")
    novnc_root()
    display_num = 100 + (os.getpid() % 1000)
    display = f":{display_num}"
    width = DESKTOP_VIEWPORT["width"]
    height = DESKTOP_VIEWPORT["height"]
    rfb_port = 20000 + (os.getpid() % 10000)
    ws_port = rfb_port + 1
    spawn(command_argv("Xvfb") + [
        display,
        "-screen", "0", f"{width}x{height}x24",
        "-nolisten", "tcp",
        "-ac",
    ])
    socket_path = f"/tmp/.X11-unix/X{display_num}"
    wait_until(lambda: os.path.exists(socket_path), 8, "Xvfb")
    spawn(command_argv("x11vnc") + [
        "-display", display,
        "-localhost",
        "-rfbport", str(rfb_port),
        "-nopw",
        "-forever",
        "-shared",
        "-noxdamage",
        "-noshm",
        "-quiet",
    ])
    wait_until(lambda: port_open(rfb_port), 8, "x11vnc")
    spawn(command_argv("websockify") + [
        "--heartbeat", "30",
        f"127.0.0.1:{ws_port}",
        f"127.0.0.1:{rfb_port}",
    ])
    wait_until(lambda: port_open(ws_port), 8, "websockify")
    for proc in DESKTOP_PROCS:
        if proc.poll() is not None:
            raise RuntimeError("The remote desktop exited before it was ready.")
    STATE["ws_port"] = ws_port
    print(f"Remote desktop ready on display {display}.", flush=True)
    return display


def stop_desktop():
    for proc in DESKTOP_PROCS:
        if proc.poll() is None:
            proc.terminate()
    for proc in DESKTOP_PROCS:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    DESKTOP_PROCS.clear()
    STATE["ws_port"] = 0


class ReauthServer(ThreadingHTTPServer):
    allow_reuse_address = True


class ReauthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        token, action, _rest = parse_route(urlparse(self.path).path)
        label = action or "page"
        if token is None:
            label = "unknown"
        if label in {"novnc", "websockify", "status"}:
            return
        print(f"reauth {self.command} {label}")

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket()
            return
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        parsed = urlparse(self.path)
        token, action, rest = parse_route(parsed.path)
        if token is None or not token_ok(token):
            send_html(self, 410, "<p>This link has expired.</p>")
            return
        if action == "" and method == "GET":
            if not parsed.path.endswith("/"):
                self.send_response(302)
                self.send_header("Location", parsed.path + "/")
                self.end_headers()
                return
            if STATE["done"]:
                send_html(self, 200, "<p>Logged in. You can close this tab.</p>")
                return
            if unlocked(self, token):
                send_html(self, 200, VIEWER_HTML)
                return
            send_html(self, 200, password_form(""))
            return
        if action == "unlock" and method == "POST":
            self._unlock(token)
            return
        if not unlocked(self, token):
            send_html(self, 401, "<p>Enter the reauth password first.</p>")
            return
        if STATE["done"]:
            send_html(self, 410, "<p>Logged in. You can close this tab.</p>")
            return
        if action == "novnc" and method == "GET":
            self._novnc(rest)
            return
        send_html(self, 404, "<p>Not found.</p>")

    def _unlock(self, token):
        raw = read_body(self)
        if raw is None:
            send_html(self, 413, "<p>Request too large.</p>")
            return
        given = parse_qs(raw.decode("utf-8", errors="replace")).get("password", [""])[0]
        if not passwords_match(given, STATE["password"]):
            STATE["failures"] += 1
            if STATE["failures"] >= 8:
                send_html(self, 410, "<p>This link has expired.</p>")
                return
            send_html(self, 401, password_form("Wrong password."))
            return
        STATE["session"] = secrets.token_urlsafe(32)
        body = b""
        self.send_response(302)
        self.send_header("Location", f"/r/{token}/")
        self.send_header(
            "Set-Cookie",
            f"reauth_session={STATE['session']}; HttpOnly; SameSite=Lax; Path=/r/{token}/",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.write(body)

    def _novnc(self, rel):
        path = novnc_file(rel)
        if path is None:
            send_html(self, 404, "<p>Not found.</p>")
            return
        ext = os.path.splitext(path)[1].lower()
        content_type = NOVNC_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as handle:
            send_bytes(self, 200, content_type, handle.read())

    def _websocket(self):
        parsed = urlparse(self.path)
        token, action, _rest = parse_route(parsed.path)
        if token is None or action != "websockify" or not token_ok(token) or not unlocked(self, token):
            send_html(self, 401, "<p>Enter the reauth password first.</p>")
            return
        if STATE["done"] or not STATE["ws_port"]:
            send_html(self, 410, "<p>Logged in. You can close this tab.</p>")
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", STATE["ws_port"]), timeout=10)
        except OSError:
            send_html(self, 503, "<p>Desktop unavailable.</p>")
            return
        query = ("?" + parsed.query) if parsed.query else ""
        lines = [
            f"GET /websockify{query} HTTP/1.1",
            f"Host: 127.0.0.1:{STATE['ws_port']}",
        ]
        for key in (
            "Upgrade",
            "Connection",
            "Sec-WebSocket-Key",
            "Sec-WebSocket-Version",
            "Sec-WebSocket-Protocol",
            "Sec-WebSocket-Extensions",
            "Origin",
        ):
            value = self.headers.get(key)
            if value:
                lines.append(f"{key}: {value}")
        try:
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            self.close_connection = True
            sockets = [self.connection, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 300)
                if not readable:
                    break
                for sock in readable:
                    other = upstream if sock is self.connection else self.connection
                    data = sock.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
        finally:
            upstream.close()


def reauth_base_url():
    base = os.environ.get("REAUTH_BASE_URL", "").rstrip("/")
    password = os.environ.get("REAUTH_PASSWORD", "")
    parsed = urlparse(base)
    if not password:
        raise RuntimeError("Set REAUTH_PASSWORD in .env before a manual login is required.")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise RuntimeError(
            "Set REAUTH_BASE_URL to an address your phone can open, including the port. "
            "Example: http://100.x.x.x:8787"
        )
    return base, parsed.port, password


def serve_reauth(page):
    global SERVER
    base, port, password = reauth_base_url()
    token = secrets.token_urlsafe(32)
    STATE.update(
        {
            "token": token,
            "expires": time.time() + LINK_TTL_SECONDS,
            "password": password,
            "session": "",
            "failures": 0,
            "done": False,
            "closed": False,
        }
    )
    try:
        SERVER = ReauthServer(("0.0.0.0", port), ReauthHandler)
    except OSError as e:
        raise RuntimeError(f"Could not listen on port {port}: {e}") from e

    thread = threading.Thread(target=SERVER.serve_forever, daemon=True)
    thread.start()
    url = f"{base}/r/{token}/"
    post_alert(
        "OHQ login needed. Open this link within 15 minutes, enter the reauth password, "
        "and sign in on the page:\n"
        + url
    )

    last_check = 0
    last_report = None
    seen_login_page = False
    context = page.context
    try:
        while True:
            if time.time() >= STATE["expires"] or STATE["failures"] >= 8:
                print("Login link expired.")
                return EXIT_EXPIRED
            time.sleep(0.5)
            now = time.time()
            if now - last_check >= 2:
                last_check = now
                header = session_header(context)
                urls = []
                ohq_page = None
                for candidate in list(context.pages):
                    try:
                        urls.append(brief_url(candidate.url))
                    except Exception:
                        urls.append("(closed)")
                        continue
                    if ohq_page is None and page_shows_ohq(candidate):
                        ohq_page = candidate
                if ohq_page is None:
                    seen_login_page = True
                cookie_state = "none"
                if header and header == PREVIOUS_COOKIE:
                    cookie_state = "same as the rejected cookie"
                elif header:
                    cookie_state = "new"
                report = (cookie_state, tuple(urls))
                if report != last_report:
                    print(f"Browser: cookie {cookie_state}; pages: {urls}", flush=True)
                    last_report = report
                fresh = header and (header != PREVIOUS_COOKIE or seen_login_page)
                if ohq_page is not None and fresh:
                    checked = live_session_header(ohq_page)
                    if checked:
                        save_cookie(checked)
                        STATE["done"] = True
                        time.sleep(2)
                        return EXIT_INTERACTIVE
                    print("OHQ page is open, but /api/user has not accepted the session yet.", flush=True)
    finally:
        STATE["closed"] = True
        SERVER.shutdown()
        SERVER.server_close()
        SERVER = None


def visible_login():
    """Open a normal browser window and wait for Enter. No saved profile or login page."""
    print("Log in with your Andrew ID + Duo in the browser window.")
    print("Once you see the actual queue/course page, come back here and press Enter.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            page.goto(OHQ_URL)
            input()
            header = session_header(page.context)
            if not header:
                raise RuntimeError("No session_id cookie found. Finish login, then press Enter.")
            save_cookie(header)
        finally:
            browser.close()
    return EXIT_INTERACTIVE


def install_signal_handlers():
    def handle(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def main():
    global PREVIOUS_COOKIE
    load_dotenv(override=True)
    if not os.environ.get("REAUTH_BASE_URL", "").strip():
        return visible_login()
    PREVIOUS_COOKIE = load_previous_cookie()
    print("Opening the saved browser...", flush=True)
    with sync_playwright() as playwright:
        context, page = open_browser(playwright)
        try:
            print("Loading OHQ...", flush=True)
            page.goto(OHQ_URL, wait_until="domcontentloaded", timeout=45000)
            header = wait_for_new_session(page, SILENT_WAIT_SECONDS)
            if not header and session_header(context):
                print("Saved session was rejected. Loading OHQ again...", flush=True)
                clear_session_cookie(context)
                page.goto(OHQ_URL, wait_until="domcontentloaded", timeout=45000)
                header = wait_for_new_session(page, SILENT_WAIT_SECONDS)
            if header:
                save_cookie(header)
                return EXIT_SILENT
            print("Saved profile is not logged in. Opening the remote desktop.", flush=True)
            context.close()
            context = None
            display = start_desktop()
            context, page = open_browser(playwright, headless=False, display=display)
            page.goto(OHQ_URL, wait_until="domcontentloaded", timeout=45000)
            if open_cmu_login(page):
                header = wait_for_new_session(page, SILENT_WAIT_SECONDS)
                if header:
                    save_cookie(header)
                    return EXIT_SILENT
            if try_saved_credentials(page):
                header = wait_for_new_session(page, SILENT_WAIT_SECONDS)
                if header:
                    save_cookie(header)
                    return EXIT_SILENT
            return serve_reauth(page)
        finally:
            if context is not None:
                context.close()
            stop_desktop()


if __name__ == "__main__":
    install_signal_handlers()
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Cookie refresh failed: {e}")
        if isinstance(e, RuntimeError):
            post_alert(f"OHQ cookie refresh failed. {e}")
        else:
            post_alert("OHQ cookie refresh failed. Check the Pi log.")
        sys.exit(EXIT_ERROR)
