# intent_server

Local sidecar that judges whether a chat message is actually talking to the
bot, using sentence-embedding similarity (`all-MiniLM-L6-v2` via `fastembed`,
CPU-only ONNX, no PyTorch/GPU) instead of an LLM call. It's an optional
refinement on top of the bot's existing reply-chance heuristics in
`LapplandV2.py` (mention/reply/name-drop/momentum/solo-speaker) -- only
consulted for the one case those can't tell apart well: implicit addressing
in an active *multi-person* channel with no mention, reply, or name-drop. If
it's unreachable or returns "uncertain", the bot falls back to the old flat
ambient chance -- this is additive, not a hard dependency. See `server.py`'s
docstring for the full contract.

Deployed the same way as `stt/`: a docker-compose service (`intent` in
`docker-compose.yml`), built from the `Dockerfile` in this directory,
reachable internally at `http://intent:8803`. Publishes no host port -- only
`lappland` (and anything else on the compose network) can reach it.

## Deploy

```
docker compose up -d --build
```

builds and starts it along with everything else. `INTENT_SERVER_URL` is
already wired into `lappland`'s environment in `docker-compose.yml`, so
nothing else needs configuring -- the bot picks it up on its next restart.

To turn it off without removing the service, comment out the
`INTENT_SERVER_URL` line in `lappland`'s environment block and restart
`lappland` -- `core/intent.py` treats an unset URL as "feature off" and falls
back to the plain heuristics.

## Tuning

Classification is few-shot, not a trained model: `server.py` holds two small
curated lists of example phrases (`POSITIVE_EXAMPLES` / `NEGATIVE_EXAMPLES`)
and compares each incoming message against both by cosine similarity. If you
notice it consistently getting a particular kind of message wrong, add a
similar real example to the appropriate list, then:

```
docker compose restart intent
```

No rebuild needed -- this directory is bind-mounted into the container (see
`volumes:` on the `intent` service), so an edit to `server.py` takes effect
on restart. The model itself is baked into the image at build time, so a
plain restart doesn't touch it.
