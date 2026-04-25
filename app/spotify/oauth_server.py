from flask import Flask, request
import requests
import json
import os

app = Flask(__name__)

SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI")  # e.g. https://yourdomain.com/callback
TOKENS_FILE           = "../../data/spotify_tokens.json"

def load_tokens() -> dict:
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_tokens(tokens: dict):
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)

@app.route("/callback")
def callback():
    code  = request.args.get("code")
    state = request.args.get("state")  # discord user ID passed through oauth flow

    if not code or not state:
        return "missing code or state", 400

    # exchange code for tokens
    response = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  SPOTIFY_REDIRECT_URI,
        "client_id":     SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    })

    if response.status_code != 200:
        return "failed to get token", 400

    data = response.json()
    tokens = load_tokens()
    tokens[state] = {  # keyed by discord user ID
        "access_token":  data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_in":    data["expires_in"],
    }
    save_tokens(tokens)
    return "linked! you can close this tab and go back to discord ✓"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)