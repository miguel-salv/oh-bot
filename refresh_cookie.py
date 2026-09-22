from playwright.sync_api import sync_playwright


def get_fresh_cookie():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://ohq.eberly.cmu.edu")

        print("Log in with your Andrew ID + Duo in the browser window.")
        print("Once you see the actual queue/course page, come back here and press Enter.")
        input()

        cookies = page.context.cookies()
        session_cookie = next(c for c in cookies if c["name"] == "session_id")
        browser.close()
        return f"session_id={session_cookie['value']}"


if __name__ == "__main__":
    cookie = get_fresh_cookie()
    with open("cookie.txt", "w") as f:
        f.write(cookie)
    print("Saved fresh cookie to cookie.txt")