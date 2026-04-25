import requests
import json
import os
import time

SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI")
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

def refresh_access_token(user_id: str) -> str | None:
    tokens = load_tokens()
    entry = tokens.get(user_id)
    if not entry:
        return None

    response = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type":    "refresh_token",
        "refresh_token": entry["refresh_token"],
        "client_id":     SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    })

    if response.status_code != 200:
        return None

    data = response.json()
    tokens[user_id]["access_token"] = data["access_token"]
    save_tokens(tokens)
    return data["access_token"]

def get_access_token(user_id: str) -> str | None:
    """always refreshes to be safe since we don't track expiry"""
    return refresh_access_token(user_id)

def get_now_playing(user_id: str) -> dict | None:
    token = get_access_token(user_id)
    if not token:
        return None

    r = requests.get("https://api.spotify.com/v1/me/player/currently-playing", headers={
        "Authorization": f"Bearer {token}"
    })

    if r.status_code == 204:  # nothing playing
        return None
    if r.status_code != 200:
        return None

    data = r.json()
    if not data or not data.get("item"):
        return None

    track = data["item"]
    return {
        "name":     track["name"],
        "artist":   ", ".join(a["name"] for a in track["artists"]),
        "album":    track["album"]["name"],
        "url":      track["external_urls"]["spotify"],
        "image":    track["album"]["images"][0]["url"] if track["album"]["images"] else None,
        "playing":  data["is_playing"],
    }

def get_recent_tracks(user_id: str, limit: int = 5) -> list:
    token = get_access_token(user_id)
    if not token:
        return []

    r = requests.get(f"https://api.spotify.com/v1/me/player/recently-played?limit={limit}", headers={
        "Authorization": f"Bearer {token}"
    })

    if r.status_code != 200:
        return []

    return [{
        "name":   item["track"]["name"],
        "artist": ", ".join(a["name"] for a in item["track"]["artists"]),
        "url":    item["track"]["external_urls"]["spotify"],
    } for item in r.json().get("items", [])]

def add_to_queue(user_id: str, track_uri: str) -> bool:
    token = get_access_token(user_id)
    if not token:
        return False

    r = requests.post(f"https://api.spotify.com/v1/me/player/queue?uri={track_uri}", headers={
        "Authorization": f"Bearer {token}"
    })
    return r.status_code == 204

def search_track(query: str) -> dict | None:
    # search uses client credentials, no user token needed
    r = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type":    "client_credentials",
        "client_id":     SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    })
    if r.status_code != 200:
        return None

    token = r.json()["access_token"]
    r = requests.get("https://api.spotify.com/v1/search", params={
        "q": query, "type": "track", "limit": 1
    }, headers={"Authorization": f"Bearer {token}"})

    if r.status_code != 200:
        return None

    items = r.json().get("tracks", {}).get("items", [])
    if not items:
        return None

    track = items[0]
    return {
        "name":   track["name"],
        "artist": ", ".join(a["name"] for a in track["artists"]),
        "url":    track["external_urls"]["spotify"],
        "uri":    track["uri"],
    }