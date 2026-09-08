# MultiAgency agentic social prototype

Hermes decides what is worth posting and writes the post. Everything in this
repo is the plumbing around it: pulling material in, handing it to Hermes with
the right context, taking the draft back, queuing it for a human, and
publishing on schedule.

**Nothing publishes without human approval.** There is no auto-post path, not
behind a flag, not in a config value. `publish_due` reads only `approved` rows,
and the only way a row becomes `approved` is a person pressing a button in the
queue. There is a test for that, and the week simulation checks it too.

## Run it

```bash
python -m venv .venv && .venv/Scripts/activate   # source .venv/bin/activate on posix
pip install -r requirements-dev.txt
cp .env.example .env
python -m multiagency.main check
python -m multiagency.main serve
```

### Open the approval queue

With `serve` running, go to **http://127.0.0.1:8000**. That page is the whole
review interface and the only way a post can ever reach `approved`.

The scheduler runs in the same process, so `serve` is the only command needed
day to day. On startup it pulls material and fills any upcoming slot, so the
queue has something in it immediately rather than after the first interval.

Out of the box `HERMES_MODE=mock` and `PUBLISHER=mock`, so the whole thing runs
end to end with a fake Hermes and writes "published" posts to
`data/published_mock.log` instead of to X. Nothing reaches the network until
you change those, and nothing publishes without a human pressing approve.

There is no login by default. Keep it on localhost, or set `UI_USERNAME` and
`UI_PASSWORD` in `.env` for basic auth.

Other commands:

```bash
python -m multiagency.main check      # validate the content config, print the lanes
python -m multiagency.main pull       # pull material once
python -m multiagency.main generate   # fill upcoming slots with pending posts
python -m multiagency.main publish     # publish anything approved and due
python -m multiagency.main status     # queue depth and material left
```

Tests, and a full week against a fake clock:

```bash
python -m pytest
python scripts/simulate_week.py
```

The simulation walks seven days an hour at a time and reports how many posts
were generated, what the reviewer approved, whether any slot was skipped, how
much material is left, and whether anything reached `posted` without a human
approval first.

## The content system

`config/content.yaml` is the whole content system: lanes, sources, constraints
and the schedule. It is meant to be edited by the person tuning the content,
not the person maintaining the code, so it is validated on load and every
problem is reported at once with the path to the offending key. A bad file
stops the process instead of starting it in a broken state.

```yaml
lanes:
  - id: field_notes
    audience: builders          # everyone | builders | projects | clients
    post_type: observation
    purpose: >
      What this lane is for, in a sentence or two.
    example: >
      A worked example of a good post in this lane.
    constraints:
      max_length: 270
      allow_emojis: false
      rules:
        - "One idea per post. No lists."
    active: true
    sources:
      - id: field_notes_log
        source_type: file_lines   # file_lines | directory | jsonl | rss
        location: data/seed/engineering_notes.txt

schedule:
  - id: field_notes_tue
    lane_id: field_notes
    day_of_week: tue
    time: "10:00"               # quote it, or YAML reads 9:00 as the number 540
    active: true
```

Lane constraints fold onto the global ones. A lane can tighten `max_length` and
can turn emojis on for itself, since the global rule is "no emojis unless the
lane explicitly allows them". It cannot re-enable em-dashes.

If the output is not good enough, the fix is the lane definition or the
constraints, not the code. That is the point of keeping generation entirely
inside Hermes.

## The Hermes contract

The full specification, for whoever builds the Hermes side, is in
[docs/hermes-contract.md](docs/hermes-contract.md). The short version:

One adapter module, `multiagency/hermes/`, is the only seam. Everything
upstream speaks `HermesRequest` and `HermesResponse`.

Hermes receives:

- the lane definition: audience, post type, purpose, worked example
- the constraints block for that lane, plus the global constraints
- the candidate source material
- the last 10 posts published, so it does not repeat itself

Hermes returns:

- `text` — the post
- `lane_id` — which lane it is for
- `material_id` — which material it drew from
- `reasoning` — one or two sentences on why this was worth posting

A response missing any of those, or naming a different lane or material, is an
error rather than something to paper over. `reasoning` is stored and shown at
the top of every card in the review UI, above the draft: a reviewer needs to
see whether Hermes picked well before judging whether it wrote well, and that
is most of what makes review fast.

`HERMES_MODE=mock` uses the built-in fake. `HERMES_MODE=live` posts the request
payload as JSON to `HERMES_ENDPOINT` and expects those four fields back. If the
real Hermes speaks a different shape, or turns out to be a Python library
rather than a service, `HttpHermes.call_raw` is the one method to change and
nothing else moves.

### Connecting the real Hermes

Two commands, neither of which writes anything:

```bash
python -m multiagency.main hermes-payload   # the exact request, sent nowhere
python -m multiagency.main hermes-check     # one real call, reported in full
```

`hermes-payload` prints the JSON this system would send. Hand it to whoever
owns Hermes to confirm the shape before pointing anything at it.

`hermes-check` builds a genuine request from real material and real published
history, sends it once, and reports the raw response, which contract fields
were satisfied, and whether the post would have passed the lane's mechanical
constraints. It queues no post and marks no material used, so a failure costs
nothing but the call. Exit codes: `0` the contract held, `1` Hermes answered
but not within the contract, `2` nothing was configured to call.

Run it until it exits `0`, then set `HERMES_MODE=live`. A constraint violation
reported by the probe is not a connection problem: it means the wiring works
and the lane needs tuning.

## The flow

1. **Pull.** A scheduled job reads every configured source into `material`,
   deduplicating on a content fingerprint so a source that republishes the same
   item under a new id does not produce a second draft. A source that is
   unreachable is recorded in the events log and does not stop the others.
2. **Generate.** For each upcoming slot inside the lead window, the oldest
   unused material for that lane is selected and handed to Hermes with the
   contract above. The result is written to `posts` as `pending`.
3. **Validate.** The returned text is checked against the mechanical
   constraints: dashes, emojis, length. On a violation Hermes is asked once
   more, from scratch, with a note saying which constraint broke. If the second
   attempt also fails, the post is queued as `pending` and **flagged**, never
   dropped. The judgment rules (no funding claims, no token names, no
   manufactured urgency) are Hermes's job through the prompt and are
   deliberately not reimplemented as a keyword blocklist.
4. **Approve.** A human opens the queue and approves, edits, or rejects. An
   edit is stored in `edited_text` and that is what publishes; the original
   draft is kept alongside it.
5. **Post.** The scheduler publishes approved posts at their slot time and
   stores `x_post_id`. On failure the post becomes `failed` with the reason on
   the row. There is no silent retry.
6. **Empty slot.** A slot that arrives with nothing approved is logged and
   skipped. The pending post stays pending for the reviewer. Nothing is
   published to fill the gap.

## Data model

`lanes`, `sources`, `material`, `posts`, `schedule` as agreed. `status` is one
of `pending`, `approved`, `rejected`, `posted`, `failed`.

Three additions, called out so they are not mistaken for drift:

- `posts.flagged` and `posts.flag_reason` carry step 3. Without somewhere to
  put it, a twice-failing post could only be dropped or silently queued.
- `posts.slot_id` ties a post to the schedule row that asked for it, which is
  what stops the generator filling the same slot twice. A rejected post frees
  its slot so the next run can try again with different material.
- `events` is an append-only operational log. Skipped slots, failed sources,
  regenerations and publish failures land there so a week can be audited after
  the fact. It is shown at the bottom of the queue page.

Lanes, sources and schedule rows are mirrored from the YAML at startup. The
YAML is the source of truth; rows that disappear from it are marked inactive
rather than deleted, because posts and material still point at them.

## Approval interface

Single page at `/`. For each pending post it shows the lane, the slot time,
Hermes's reasoning, the source material it drew from, the draft in an editable
box, and a live character count against that lane's limit. Three actions:
approve, save edit and keep pending, reject. Approved items show their slot
times, posted items link out, failures sit at the top of the page, and flagged
posts have an amber border and a banner saying which constraint broke.

Approving text over X's 280 character limit is refused; the edit is saved and
the post stays pending, so the work is not lost.

No login by default. Run it on localhost, or set `UI_USERNAME` and
`UI_PASSWORD` for basic auth.

## Going live

Wire the real integrations last, and one at a time.

- **Hermes.** Set `HERMES_MODE=live` and `HERMES_ENDPOINT`. Run
  `python -m multiagency.main generate` and read the queue before trusting the
  scheduler with it.
- **X.** Set `PUBLISHER=x` and the four `X_*` credentials in `.env`. They are
  read in `settings.py` and used only in `publisher.py`; they are never stored
  in the database, never logged, and never rendered in the UI. `.env` is
  gitignored.

OAuth 1.0a is signed by `requests-oauthlib`. The body is sent as JSON, so
oauthlib signs only the `oauth_*` parameters and leaves the body out of the
signature base string, which is what the v2 endpoint expects. There is a test
that pins this: two different bodies signed with the same nonce and timestamp
must produce the same signature.

## Dependencies

`pyyaml`, `apscheduler`, `fastapi`, `uvicorn`, `python-multipart` (FastAPI
needs it to read HTML form posts), `requests` and `requests-oauthlib`.
`pytest` and `httpx` for tests.

All HTTP goes through `requests`: the Hermes adapter, the X publisher and the
RSS reader. The .env reader, the feed parsing and the HTML rendering are
standard library.

## Out of scope

No autonomous posting, replies, DMs, community response, multiple accounts,
analytics, threads or media attachments. Single text posts only. If something
looks like it needs one of those, it should be raised rather than built.
