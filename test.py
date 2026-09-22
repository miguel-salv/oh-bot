import requests

with open("cookie.txt") as f:
    SESSION_COOKIE = f.read().strip()

resp = requests.get(
    "https://ohq.eberly.cmu.edu/user",
    headers={"Cookie": SESSION_COOKIE},
)
print(resp.status_code)
print(resp.text[:500])