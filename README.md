# karaoke-queue

A house karaoke queue: guests request songs from their phones, admins control
the order, and a server drives the YouTube app on the TV. Guests never connect
to the TV, so the TV's Premium account stays ad-free (to be confirmed in Phase 0).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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
