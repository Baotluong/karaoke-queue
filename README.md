# karaoke-queue

A house karaoke queue: guests request songs from their phones, admins control
the order, and a server drives the YouTube app on the TV. Guests never connect
to the TV, so the TV's Premium account stays ad-free (confirmed in Phase 0).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

## Run it

1. Pair with the TV once (see Phase 0 below). This saves `.lounge_auth.json`.
2. Start the server so phones on your Wi-Fi can reach it:
   ```bash
   .venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
   ```
3. Guests open `http://<this computer's IP>:8000` on their phones.
   On a Mac: `ipconfig getifaddr en0` shows the IP.

Without a TV (development): `KQ_FAKE_TV=1 KQ_FAKE_SONG_SECONDS=15` before the
command fakes a TV whose songs last 15 seconds.

### Settings (environment variables or a `.env` file)

| Variable | Default | Meaning |
|---|---|---|
| `YT_API_KEY` | none | YouTube Data API key. Without it, guests paste YouTube links instead of searching. |
| `KQ_DB` | `karaoke.db` | SQLite database file |
| `KQ_FAKE_TV` | off | Use a pretend TV |
| `KQ_FAKE_SONG_SECONDS` | `20` | Song length on the fake TV |

### Tests

```bash
.venv/bin/pytest
```

## Phase 0: ad spike

1. Make sure the TV's YouTube app is signed into the Premium account.
2. Pair once (TV: Settings > Link with TV code):
   ```bash
   .venv/bin/python spike/ad_check.py pair
   ```
3. Play test videos and watch for ads:
   ```bash
   .venv/bin/python spike/ad_check.py run
   ```

## Roadmap

- **Phase 0:** ad spike (Lounge API + Premium TV)
- **Phase 1:** FastAPI + SQLite web app: guest search and request, live queue, player loop
- **Phase 2:** admin PIN, reorder, skip, remove, block, optional approval
- **Phase 3:** round-robin fairness, per-singer limits
- **Phase 4:** house-only access: nightly join code, per-device cookie
- **Phase 5:** party polish
