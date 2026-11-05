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

Admins open `http://<IP>:8000/admin` and enter the PIN to skip, pause/resume,
reorder, remove, play a song now, or block a singer (which also removes their
waiting songs). Share the PIN with the few people who should run the night.

**Fair turns (on by default):** everyone takes turns. Only the song playing now
and songs still waiting count; songs already sung don't count against anyone.
Each person's 1st waiting song is in round 1, their 2nd in round 2, and so on,
and a new song goes at the end of its round, so newcomers go ahead of other
people's second songs and nobody sings twice in a row while others are waiting.
Admins can still reorder by hand, and can turn fair turns off under *House
rules* on the admin page (new songs then go to the bottom).

In both modes each person can have up to 3 songs waiting (changeable under
*House rules*; 0 = no limit), the same song can't be queued twice, and guests
can remove their own waiting songs.

Without a TV (development): `KQ_FAKE_TV=1 KQ_FAKE_SONG_SECONDS=15` before the
command fakes a TV whose songs last 15 seconds.

### Settings (environment variables or a `.env` file)

| Variable | Default | Meaning |
|---|---|---|
| `YT_API_KEY` | none | YouTube Data API key. Without it, guests paste YouTube links instead of searching. |
| `KQ_ADMIN_PIN` | random each run (printed in the log) | PIN for the admin page at `/admin` |
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
- **Phase 3:** round-robin fairness, per-singer limits, no duplicate songs
- **Phase 4:** house-only access: nightly join code, per-device cookie
- **Phase 5:** party polish
