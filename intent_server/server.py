"""
intent_server.server -- lightweight "is this message talking to the bot?"
classifier.

Runs as its own docker-compose service (see the `intent` entry in
docker-compose.yml, and Dockerfile in this directory) alongside the bot and
`stt`, reachable internally at http://intent:8803 -- no network calls of its
own, pure local CPU inference via fastembed (ONNX runtime, no PyTorch/CUDA
needed) using the all-MiniLM-L6-v2 sentence embedding model (~90MB, baked
into the image at build time, ~10-30ms/message on CPU). Idle cost after
startup is just the model sitting in RAM.

How it decides: embeds the message and compares it (cosine similarity)
against two small curated sets of example phrases -- POSITIVE (things people
say when talking TO the bot without naming it: "you good?", "explain that
again") and NEGATIVE (ambient banter between other people: "bro did you see
the game", "anyone free later"). Whichever side it's closer to wins, but only
if the gap clears MARGIN -- otherwise it returns null ("uncertain") rather
than guessing, since the caller (core/intent.py) falls back to its own
reply-chance heuristics on anything that isn't a confident yes/no. This
server is additive, not authoritative: it should never be the only thing
deciding whether the bot replies.

Endpoints:
    GET  /health     -- liveness check
    POST /classify    -- {"text": "..."} -> {"directed_at_bot": true|false|null, "score": float}

Config (env vars):
    INTENT_SERVER_API_KEY  -- optional. If set, requests must send a
                              matching X-Api-Key header. Not needed for the
                              default compose setup, since this container
                              publishes no host port -- only reachable from
                              other services on the compose network.
    INTENT_SERVER_PORT     -- defaults to 8803.
"""

import os

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

API_KEY = os.environ.get("INTENT_SERVER_API_KEY")
PORT = int(os.environ.get("INTENT_SERVER_PORT", "8803"))

# Gap required between the best positive-side and best negative-side
# similarity before committing to a verdict. Below this, "uncertain" (null)
# is returned -- tuned against a small hand-labeled test set where 0.03 gave
# zero confidently-wrong verdicts (see conversation/testing notes); raise it
# to make the server more conservative, lower it to have it commit more often.
MARGIN = 0.03

# Few-shot reference phrases, not a trained classifier -- add more of either
# as you notice real misclassifications in your server's actual channels.
# Keep these as natural, varied phrasing rather than keyword lists; embedding
# similarity is comparing meaning/register, not vocabulary overlap.
POSITIVE_EXAMPLES = [
    "what do you think about that",
    "you good?",
    "wyd",
    "youre so annoying lol",
    "explain that again",
    "fr? really?",
    "no youre wrong about that",
    "lmaooo youre unhinged",
    "wait what did you mean by that",
    "do you even remember what we talked about",
    "youre kind of a jerk sometimes ngl",
    "can you help me with something",
    "thoughts?",
    "youre right actually",
    "be honest with me",
    "youre wrong and you know it",
    "be real with me for a sec",
    "thats not what i asked you",
    "youre being weird right now",
    "do you ever get tired of this",
]

NEGATIVE_EXAMPLES = [
    "bro did you see the game last night",
    "yeah im down for that",
    "guys should we get food",
    "anyone free later tonight",
    "that guy from work is so annoying",
    "my internet keeps cutting out",
    "lol thats crazy",
    "idk man just vibes",
    "she said shed be here at 8",
    "did everyone finish the assignment",
    "my code keeps crashing",
    "this weather is insane today",
    "i think i left my keys at the office",
    "we should plan the trip soon",
    "he never texts back smh",
    "my wifi is trash rn",
    "should we order pizza",
    "anyone know what time it starts",
]

app = FastAPI()
_model = None
_pos_emb: np.ndarray | None = None
_neg_emb: np.ndarray | None = None


@app.on_event("startup")
def _load_model():
    global _model, _pos_emb, _neg_emb
    from fastembed import TextEmbedding
    _model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    _pos_emb = np.array(list(_model.embed(POSITIVE_EXAMPLES)))
    _neg_emb = np.array(list(_model.embed(NEGATIVE_EXAMPLES)))
    print(f"[intent_server] ready ({len(POSITIVE_EXAMPLES)} positive / {len(NEGATIVE_EXAMPLES)} negative examples)", flush=True)


def _check_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad or missing API key")


def _cosine_max(query: np.ndarray, refs: np.ndarray) -> float:
    sims = refs @ query / (np.linalg.norm(refs, axis=1) * np.linalg.norm(query))
    return float(sims.max())


class ClassifyRequest(BaseModel):
    text: str


@app.get("/health")
def health(x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return {"status": "ok"}


@app.post("/classify")
def classify(req: ClassifyRequest, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)

    text = req.text.strip()
    if not text:
        return {"directed_at_bot": None, "score": 0.0}

    embedding = np.array(list(_model.embed([text])))[0]
    pos_score = _cosine_max(embedding, _pos_emb)
    neg_score = _cosine_max(embedding, _neg_emb)
    diff = pos_score - neg_score

    if diff > MARGIN:
        verdict = True
    elif diff < -MARGIN:
        verdict = False
    else:
        verdict = None

    return {"directed_at_bot": verdict, "score": diff}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
