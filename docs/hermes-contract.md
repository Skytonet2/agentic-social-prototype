# The Hermes contract

This is the specification for the Hermes side of the connection. Everything in
the social prototype is already written against it; this document is what the
Hermes implementation has to satisfy.

Hermes is the reasoning layer. It decides whether the material in front of it
is worth posting and it writes the post. Nothing on the prototype side
generates text, chains prompts, calls a second model, or edits what comes back
beyond mechanical validation. That is deliberate: if the output is wrong, the
fix is the lane definition or the constraints, and those live in a YAML file
that a non-engineer can edit. Any generation logic that leaks out of Hermes
breaks that property.

## The shape of the exchange

One call, one post. No conversation, no state held between calls, no tool use
required on the Hermes side.

```
  request  ──▶  Hermes  ──▶  response
   lane                       text
   constraints                lane_id
   material                   material_id
   recent_posts               reasoning
   images                     image_prompt   (optional)
   retry_note                 image_alt      (with image_prompt)
```

Default transport is `POST` with a JSON body and a JSON response.
`HERMES_ENDPOINT` names the URL and `HERMES_API_KEY`, if set, is sent as
`Authorization: Bearer <key>`. If Hermes ends up as a Python library or a CLI
instead, only `HttpHermes.call_raw` in `multiagency/hermes/adapter.py` changes;
the contract below is unaffected.

## What Hermes receives

To see the exact request for your own config at any time:

```bash
python -m multiagency.main hermes-payload
```

### `lane` — who this is for and what good looks like

| field | type | meaning |
| --- | --- | --- |
| `id` | string | Lane identifier. Must be echoed back in `lane_id`. |
| `audience` | string | One of `everyone`, `builders`, `projects`, `clients`. |
| `post_type` | string | The kind of post this lane runs, e.g. `observation`. |
| `purpose` | string | What the lane is for, in prose. The main steer. |
| `example` | string | A worked example of a good post in this lane. |

`purpose` and `example` are the tuning surface. When output quality is wrong,
these are what get edited, not code.

### `constraints` — the rules for this post

| field | type | meaning |
| --- | --- | --- |
| `max_length` | int | Hard character limit. Never above 280. |
| `allow_emojis` | bool | Almost always `false`. |
| `forbid_em_dash` | bool | Always `true` in practice. |
| `rules` | string[] | Global rules followed by this lane's own, already merged. |

The prototype checks three of these mechanically after the fact: dashes,
emojis, and length. The rest of `rules` is judgment and is Hermes's
responsibility. They are deliberately not reimplemented as a keyword blocklist,
because that fails in both directions.

### `material` — the candidate

| field | type | meaning |
| --- | --- | --- |
| `id` | int | Must be echoed back in `material_id`. |
| `source_id` | string | Which configured source it came from. |
| `content` | string | The raw material. Unsummarised, unranked. |

One candidate per call. Selection already happened; the judgment left to Hermes
is whether this is worth posting and how to write it.

### `recent_posts` — the last ten published, newest first

| field | type | meaning |
| --- | --- | --- |
| `lane_id` | string | Which lane it went out in. |
| `text` | string | What actually published, including any human edit. |
| `posted_at` | string | UTC, `YYYY-MM-DDTHH:MM:SSZ`. |

This is the repetition guard, and it is the single most important field for a
system that runs unattended for a week. Without it, output converges on the
same three observations by about day four. Empty on a fresh install.

### `images` — whether this lane takes a picture

| field | type | meaning |
| --- | --- | --- |
| `allowed` | bool | Whether this lane accepts images at all. |
| `alt_text_max` | int | Character cap on alt text. X allows 1000. |

`allowed` comes from two switches in the content YAML: a global `images.enabled`
and a per-lane `allow_images`. Both must be true. When it is `false`, do not
return an `image_prompt`; one that arrives anyway is dropped and recorded
rather than quietly honoured.

### `retry_note` — usually `null`

Set only on the one regeneration the prototype allows, and only when the
previous attempt broke a **mechanical** constraint. It names what broke, for
example:

```
"The previous attempt broke a hard constraint: contains an em dash.
 Write a fresh post from the same material that does not."
```

This is not a rewrite instruction and not prompt chaining. Generate afresh from
the same material. There is never more than one retry.

## What Hermes must return

```json
{
  "text": "Dedupe on content, not on identifier. Half of what we pulled twice came back with a new id and the same words, and the system cheerfully wrote about it again.",
  "lane_id": "field_notes",
  "material_id": 3,
  "reasoning": "The material names a specific failure mode with a concrete fix, which is what this lane is for, and nothing in the last ten posts covers deduplication."
}
```

| field | type | rejected if |
| --- | --- | --- |
| `text` | string | missing, not a string, empty or whitespace only |
| `lane_id` | string | missing, or not the `lane.id` that was sent |
| `material_id` | int | missing, non-numeric, or not the `material.id` that was sent |
| `reasoning` | string | missing, not a string, empty or whitespace only |
| `image_prompt` | string or absent | present but not a string |
| `image_alt` | string | `image_prompt` present and this missing, empty, or over the cap |

A response failing any of these raises an error. The prototype queues nothing,
leaves the material unused so it can be tried again, and records the failure.
It does not guess, retry silently, or fall back to a default.

### `reasoning` is not optional garnish

It is shown at the top of every card in the review queue, above the draft. A
reviewer reads it before reading the post, because judging whether the pick was
right takes two seconds and judging whether the writing is right takes twenty.
It is most of what makes review fast enough to be sustainable.

One or two sentences. Say why this material was worth posting, in this lane,
now. "It is interesting" is not reasoning. Referring to what the recent posts
did or did not cover is exactly what it is for.

## Images

Optional, and only where `images.allowed` is true. Hermes decides whether a
post is better with a picture; most are not. When it decides yes, it returns
both fields together:

```json
{
  "text": "The approval queue now shows the reasoning behind each draft.",
  "lane_id": "shipped",
  "material_id": 41,
  "reasoning": "It is a concrete shipped change with a measurable effect, and nothing in the last ten posts covers the review interface.",
  "image_prompt": "A restrained editorial illustration of a review queue with a single gate. Flat shapes, muted palette, no text, no logos.",
  "image_alt": "An illustration of a queue of cards passing through one gate"
}
```

The image model downstream only executes `image_prompt`. It does not interpret
it, embellish it, or decide anything. If the picture is wrong, the prompt was
wrong, and the prompt is Hermes's.

**`image_alt` is required whenever `image_prompt` is present.** An image
published without alt text is inaccessible, so the pair is refused rather than
posted incomplete. Write the alt text for someone who cannot see the image, not
as a restatement of the prompt.

Two things are worth knowing about what happens next. A failed render never
costs the post: the text is already queued and the reason is stored beside it,
so the post goes to review as text. And the reviewer sees the image at a size
worth judging and can drop it, publishing the words alone. An image is a
suggestion; the post is the deliverable.

## Errors

On failure, return a non-2xx status with any body. The body is captured
verbatim into the operational log and shown in the queue, so a useful message
is worth sending. Do not return 200 with an error object in it: that reads as a
contract violation rather than an outage, and the two get handled differently.

## What happens to the answer

1. The text is checked against `max_length`, emoji and dash rules.
2. On a violation, Hermes is called once more with `retry_note` set.
3. If the second answer also violates, the post is queued anyway and **flagged**
   for the reviewer with the reason. Nothing is ever silently dropped.
4. A human approves, edits or rejects it. Only then can it publish.

Nothing publishes without human approval. Hermes does not post, does not
schedule, and never sees a credential.

## If the response shape differs

Very likely, since Hermes will wrap a model and models come with envelopes.
Mapping belongs in one place: `HttpHermes.call_raw` in
`multiagency/hermes/adapter.py`, which returns the raw decoded body. Override
it to unwrap, and nothing else in the system moves:

```python
class WrappedHermes(HttpHermes):
    """Hermes behind an OpenAI-style envelope."""

    def call_raw(self, request):
        raw = super().call_raw(request)
        return json.loads(raw["choices"][0]["message"]["content"])
```

`tests/test_probe.py` and `tests/test_hermes_adapter.py` cover this seam. To see
what an unmapped response looks like against a live endpoint:

```bash
python -m multiagency.main hermes-check
```

It sends one real request and prints the raw response next to a field-by-field
contract check. It writes nothing: no post is queued and no material is marked
used, so a failed attempt costs only the call.

## A full worked request

Taken from the shipped config, with two posts already published.

```json
{
  "lane": {
    "id": "field_notes",
    "audience": "builders",
    "post_type": "observation",
    "purpose": "Share one concrete thing learned while building agents this week. The reader should come away knowing something they did not know, not knowing that MultiAgency exists.",
    "example": "Agents that retry silently are worse than agents that fail loudly. We spent two days chasing a bug that turned out to be a retry swallowing a 401. Now every failure writes a reason to the row."
  },
  "constraints": {
    "max_length": 270,
    "allow_emojis": false,
    "forbid_em_dash": true,
    "rules": [
      "Do not use em-dashes.",
      "Do not make claims about funding, grants, or bounties.",
      "Do not name tokens, quote prices, or predict returns.",
      "No manufactured urgency. No engagement bait. No 'this changes everything'.",
      "Do not use emojis.",
      "Write plainly. If the material does not support a point, do not make one.",
      "First person plural. We, not I.",
      "One idea per post. No lists.",
      "Name the specific thing. No 'a client', say what the system did."
    ]
  },
  "material": {
    "id": 3,
    "source_id": "field_notes_log",
    "content": "Dedupe on content, not on identifier. Half of what we pulled twice had a new id and the same words, and the agent happily wrote the same post about it a second time."
  },
  "recent_posts": [
    {
      "lane_id": "field_notes",
      "text": "The bottleneck in agent work is almost never the model. It is the material going in. We spent a week tuning a prompt that was fine and one afternoon fixing the feed that was guessing.",
      "posted_at": "2026-09-09T18:23:58Z"
    },
    {
      "lane_id": "field_notes",
      "text": "Agents that retry silently are worse than agents that fail loudly. We lost two days to a retry loop swallowing a 401, and the fix was making every failure write a reason to the row it belongs to.",
      "posted_at": "2026-09-08T18:23:58Z"
    }
  ],
  "images": {
    "allowed": false,
    "alt_text_max": 1000
  },
  "retry_note": null
}
```

## Non-goals

Hermes is not asked to select material, schedule anything, publish anything,
render its own images, reply to anyone, or hold state between calls. It receives one candidate and
answers with one post and its reasoning. Keeping it that narrow is what makes
the rest of the system debuggable.
