"""The approval page, rendered as one HTML document.

Hand rendered rather than templated: it is a single page and the escaping is
explicit at every insertion point. Nothing here talks to the database.
"""

from __future__ import annotations

import sqlite3
from html import escape
from typing import Any, Iterable

from .clock import local_str, parse
from .config import ContentConfig
from .publisher import post_url

CSS = """
:root {
  --bg: #12141a; --panel: #1a1d26; --line: #2b303d; --text: #e6e8ee;
  --muted: #939aab; --accent: #7aa2f7; --good: #6fcf97; --warn: #e5b567;
  --bad: #e06c75;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 5rem; background: var(--bg); color: var(--text);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
header {
  border-bottom: 1px solid var(--line); padding: 1.25rem 1.5rem;
  display: flex; flex-wrap: wrap; gap: 1rem; align-items: baseline;
}
header h1 { font-size: 1.05rem; margin: 0; font-weight: 600; letter-spacing: .01em; }
header .env { color: var(--muted); font-size: .82rem; }
main { max-width: 60rem; margin: 0 auto; padding: 0 1.5rem; }
h2 {
  font-size: .8rem; text-transform: uppercase; letter-spacing: .09em;
  color: var(--muted); margin: 2.25rem 0 .75rem; font-weight: 600;
}
h2 .count { color: var(--text); }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 1rem 1.1rem; margin-bottom: .85rem;
}
.card.flagged { border-color: var(--warn); border-left-width: 4px; }
.card.failed { border-color: var(--bad); border-left-width: 4px; }
.meta {
  display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: center;
  color: var(--muted); font-size: .82rem; margin-bottom: .7rem;
}
.tag {
  background: #232734; border: 1px solid var(--line); border-radius: 999px;
  padding: .08rem .55rem; color: var(--text); font-size: .76rem;
}
.tag.warn { border-color: var(--warn); color: var(--warn); }
.tag.bad { border-color: var(--bad); color: var(--bad); }
.banner {
  background: #2a2418; border: 1px solid var(--warn); color: var(--warn);
  border-radius: 6px; padding: .5rem .7rem; font-size: .85rem; margin-bottom: .7rem;
}
.banner.bad { background: #2a1a1c; border-color: var(--bad); color: var(--bad); }
textarea {
  width: 100%; min-height: 5.5rem; background: #0e1015; color: var(--text);
  border: 1px solid var(--line); border-radius: 6px; padding: .7rem .8rem;
  font: inherit; resize: vertical;
}
textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.counter { font-size: .78rem; color: var(--muted); margin: .35rem 0 .75rem; }
.counter.over { color: var(--bad); font-weight: 600; }
.block { margin: .75rem 0; }
.block .label {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); margin-bottom: .2rem;
}
.reasoning { border-left: 2px solid var(--accent); padding-left: .7rem; }
.material {
  border-left: 2px solid var(--line); padding-left: .7rem; color: var(--muted);
  font-size: .88rem;
}
.text { white-space: pre-wrap; }
.actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .9rem; }
button {
  font: inherit; border-radius: 6px; padding: .42rem .95rem; cursor: pointer;
  border: 1px solid var(--line); background: #232734; color: var(--text);
}
button:hover { border-color: var(--accent); }
button.primary { background: var(--good); border-color: var(--good); color: #10231a; font-weight: 600; }
button.danger { background: transparent; border-color: var(--bad); color: var(--bad); }
a { color: var(--accent); }
.empty { color: var(--muted); font-size: .9rem; padding: .6rem 0 .2rem; }
table { width: 100%; border-collapse: collapse; font-size: .84rem; }
td, th { text-align: left; padding: .3rem .6rem .3rem 0; vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
tr + tr td { border-top: 1px solid var(--line); }
.log { color: var(--muted); font-size: .8rem; }
.log .kind { color: var(--text); }
"""

COUNTER_JS = """
document.querySelectorAll('form[data-limit]').forEach(function (form) {
  var area = form.querySelector('textarea');
  var out = form.querySelector('.counter');
  var limit = parseInt(form.dataset.limit, 10);
  function update() {
    var n = area.value.trim().length;
    out.textContent = n + ' / ' + limit + ' characters';
    out.classList.toggle('over', n > limit);
  }
  area.addEventListener('input', update);
  update();
});
"""


def _e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _lane_tags(cfg: ContentConfig, lane_id: str) -> str:
    try:
        lane = cfg.lane(lane_id)
    except KeyError:
        return '<span class="tag">{}</span>'.format(_e(lane_id))
    return (
        '<span class="tag">{}</span><span class="tag">{}</span>'
        '<span class="tag">{}</span>'.format(
            _e(lane.id), _e(lane.audience), _e(lane.post_type.replace("_", " "))
        )
    )


def _material_block(row: sqlite3.Row) -> str:
    if not row["material_content"]:
        return ""
    return (
        '<div class="block"><div class="label">Source material'
        ' &middot; {}</div><div class="material text">{}</div></div>'.format(
            _e(row["material_source"]), _e(row["material_content"])
        )
    )


def _reasoning_block(row: sqlite3.Row) -> str:
    return (
        '<div class="block"><div class="label">Why Hermes picked this</div>'
        '<div class="reasoning text">{}</div></div>'.format(
            _e(row["reasoning"] or "no reasoning returned")
        )
    )


def _pending_card(row: sqlite3.Row, cfg: ContentConfig) -> str:
    post_id = row["id"]
    text = row["edited_text"] or row["generated_text"]
    limit = cfg.constraints_for(row["lane_id"]).max_length if _has_lane(cfg, row["lane_id"]) else 280
    when = local_str(parse(row["scheduled_for"]), cfg.timezone)
    flagged = bool(row["flagged"])
    missed = row["is_missed"] if "is_missed" in row.keys() else False

    banner = ""
    if flagged:
        banner += (
            '<div class="banner">Flagged: this still {} after one regeneration. '
            "It was queued rather than dropped. Edit it or reject it."
            "</div>".format(_e(row["flag_reason"] or "broke a hard constraint"))
        )
    if missed:
        banner += (
            '<div class="banner">Its slot has already passed, so the slot was '
            "skipped. Approving it now will not publish it late.</div>"
        )

    edited_note = ""
    if row["edited_text"]:
        edited_note = (
            '<div class="block"><div class="label">Original draft</div>'
            '<div class="material text">{}</div></div>'.format(_e(row["generated_text"]))
        )

    return """
<article class="card{flag_class}" id="post-{pid}">
  <div class="meta">{tags}<span>slot {when}</span><span>#{pid}</span>{flag_tag}</div>
  {banner}
  {reasoning}
  {material}
  {edited_note}
  <form method="post" action="/posts/{pid}/approve" data-limit="{limit}">
    <div class="label">Post</div>
    <textarea name="text" aria-label="post text">{text}</textarea>
    <div class="counter"></div>
    <div class="actions">
      <button class="primary" type="submit">Approve</button>
      <button type="submit" formaction="/posts/{pid}/edit">Save edit, keep pending</button>
      <button class="danger" type="submit" formaction="/posts/{pid}/reject"
              formnovalidate>Reject</button>
    </div>
  </form>
</article>
""".format(
        pid=post_id,
        flag_class=" flagged" if flagged else "",
        tags=_lane_tags(cfg, row["lane_id"]),
        when=_e(when or "unscheduled"),
        flag_tag='<span class="tag warn">flagged</span>' if flagged else "",
        banner=banner,
        reasoning=_reasoning_block(row),
        material=_material_block(row),
        edited_note=edited_note,
        limit=limit,
        text=_e(text),
    )


def _has_lane(cfg: ContentConfig, lane_id: str) -> bool:
    try:
        cfg.lane(lane_id)
        return True
    except KeyError:
        return False


def _approved_card(row: sqlite3.Row, cfg: ContentConfig) -> str:
    text = row["edited_text"] or row["generated_text"]
    return """
<article class="card">
  <div class="meta">{tags}<span>publishes {when}</span><span>#{pid}</span>
    <span class="tag">{n} chars</span>{edited}</div>
  <div class="text">{text}</div>
  <form method="post" action="/posts/{pid}/unapprove" class="actions">
    <button type="submit">Return to pending</button>
  </form>
</article>
""".format(
        tags=_lane_tags(cfg, row["lane_id"]),
        when=_e(local_str(parse(row["scheduled_for"]), cfg.timezone)),
        pid=row["id"],
        n=len(text.strip()),
        edited='<span class="tag">edited</span>' if row["edited_text"] else "",
        text=_e(text),
    )


def _posted_row(row: sqlite3.Row, cfg: ContentConfig) -> str:
    url = post_url(row["x_post_id"])
    link = (
        '<a href="{}" target="_blank" rel="noopener">{}</a>'.format(_e(url), _e(row["x_post_id"]))
        if url
        else _e(row["x_post_id"] or "")
    )
    text = row["edited_text"] or row["generated_text"]
    return "<tr><td>{}</td><td>{}</td><td class='text'>{}</td><td>{}</td></tr>".format(
        _e(local_str(parse(row["posted_at"]), cfg.timezone)),
        _e(row["lane_id"]),
        _e(text),
        link,
    )


def _failed_card(row: sqlite3.Row, cfg: ContentConfig) -> str:
    text = row["edited_text"] or row["generated_text"]
    return """
<article class="card failed">
  <div class="meta">{tags}<span>slot {when}</span><span>#{pid}</span>
    <span class="tag bad">failed</span></div>
  <div class="banner bad">{reason}</div>
  <div class="text">{text}</div>
  <form method="post" action="/posts/{pid}/unapprove" class="actions">
    <button type="submit">Return to pending</button>
  </form>
</article>
""".format(
        tags=_lane_tags(cfg, row["lane_id"]),
        when=_e(local_str(parse(row["scheduled_for"]), cfg.timezone)),
        pid=row["id"],
        reason=_e(row["failure_reason"] or "no reason recorded"),
        text=_e(text),
    )


def _section(title: str, count: int | None, body: str, empty: str) -> str:
    """A count is shown only where it is a queue depth worth watching."""
    label = "" if count is None else ' <span class="count">{}</span>'.format(count)
    return "<section><h2>{}{}</h2>{}</section>".format(
        _e(title), label, body or '<p class="empty">{}</p>'.format(_e(empty))
    )


def render_queue(
    *,
    cfg: ContentConfig,
    pending: Iterable[sqlite3.Row],
    approved: Iterable[sqlite3.Row],
    posted: Iterable[sqlite3.Row],
    failed: Iterable[sqlite3.Row],
    material_counts: Iterable[sqlite3.Row],
    events: Iterable[sqlite3.Row],
    publisher_name: str,
    hermes_name: str,
) -> str:
    pending = list(pending)
    approved = list(approved)
    posted = list(posted)
    failed = list(failed)

    failed_html = "".join(_failed_card(r, cfg) for r in failed)
    pending_html = "".join(_pending_card(r, cfg) for r in pending)
    approved_html = "".join(_approved_card(r, cfg) for r in approved)
    posted_html = (
        "<table><tr><th>Posted</th><th>Lane</th><th>Text</th><th>Post id</th></tr>"
        + "".join(_posted_row(r, cfg) for r in posted)
        + "</table>"
        if posted
        else ""
    )

    material_html = (
        "<table><tr><th>Lane</th><th>Unused</th><th>Total pulled</th></tr>"
        + "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _e(r["lane_id"]), _e(r["unused"] or 0), _e(r["total"] or 0)
            )
            for r in material_counts
        )
        + "</table>"
    )

    events_html = "".join(
        '<div class="log"><span class="kind">{}</span> &middot; {} &middot; {}</div>'.format(
            _e(e["kind"]), _e(local_str(parse(e["at"]), cfg.timezone)), _e(e["detail"])
        )
        for e in events
    )

    body = "".join(
        [
            _section("Failed", len(failed), failed_html, "Nothing has failed."),
            _section(
                "Pending review",
                len(pending),
                pending_html,
                "Nothing waiting. Slots with no approved post will be skipped.",
            ),
            _section(
                "Approved and scheduled",
                len(approved),
                approved_html,
                "Nothing approved yet.",
            ),
            _section("Posted", len(posted), posted_html, "Nothing published yet."),
            _section("Material", None, material_html, ""),
            _section("Recent events", None, events_html, "No events yet."),
        ]
    )

    return """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MultiAgency approval queue</title>
<style>{css}</style>
</head><body>
<header>
  <h1>MultiAgency approval queue</h1>
  <span class="env">Hermes: {hermes} &middot; publisher: {publisher} &middot;
  times in {tz} &middot; nothing publishes without approval</span>
</header>
<main>{body}</main>
<script>{js}</script>
</body></html>""".format(
        css=CSS,
        js=COUNTER_JS,
        hermes=_e(hermes_name),
        publisher=_e(publisher_name),
        tz=_e(cfg.timezone),
        body=body,
    )
