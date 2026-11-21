"""Store Andrew credentials in the current user's system keyring."""
import getpass
import os
import sys

import keyring
from dotenv import set_key

from refresh_cookie import KEYRING_SERVICE, clear_login_attempt


def main():
    username = input("Andrew ID (without @andrew.cmu.edu): ").strip()
    if not username or "@" in username:
        sys.exit("Enter your Andrew ID without the email domain.")
    password = getpass.getpass("Andrew password (not displayed): ")
    if not password:
        sys.exit("Password cannot be empty.")
    try:
        keyring.set_password(KEYRING_SERVICE, username, password)
        if keyring.get_password(KEYRING_SERVICE, username) != password:
            sys.exit("Credential store verification failed.")
    except Exception as exc:
        sys.exit(f"Credential store unavailable ({type(exc).__name__}). See README setup.")
    if not os.path.isfile(".env"):
        sys.exit("Password stored, but .env was not found. Run this from the bot directory.")
    set_key(".env", "ANDREW_ID", username)
    clear_login_attempt()
    print("Password stored in the keyring and Andrew ID saved in .env.")
    print("The bot must run as this same user with access to this credential store.")


if __name__ == "__main__":
    main()
