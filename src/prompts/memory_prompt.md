# Weekly Memory

The weekly report for {week_label} is finished and is going to the user exactly
as written below. Your only job is to decide what, if anything, should be
carried forward from it.

What you write here is replayed into every later prompt — reports, coaching
reviews, nudges — for weeks. The user never sees it, so nobody will catch a
wrong line. That asymmetry is the whole reason this is a separate step: a
mistake here is quiet and long-lived, while an omission costs nothing.

## The report that was sent

{report}

## Shared Review Facts

{review_facts}

## Recent User Notes

{log}

## What is already in memory

{history}

---

## Instructions

Return **only** a `<memory>` block with **at most two bullets**. No preamble,
no explanation, nothing after the closing tag.

Store only what the database cannot recompute:

- a commitment the report made visible to the user
- an open thread — something flagged that has not resolved
- a causal attribution the user's own notes support

Never store:

- **Weekly counts, distances, averages, sleep or HRV figures.** The next
  report queries the database directly and gets better numbers than these.
- **A prescription that is not in the report above.** The user cannot see this
  block, so a standard recorded only here is one they are held to without ever
  being told.
- **Anything already in "What is already in memory".** Repeating it wastes the
  slots; if a thread is still open, that entry is already carrying it.
- **An inference stated as settled fact.** If the notes support it, write it
  plainly; if you are reasoning from the numbers alone, either qualify it
  ("looks like", "tracks with") or leave it out.

Fewer bullets is better than padded ones. If the week carried nothing forward
— and an unremarkable week usually does not — return an empty block:

<memory>
</memory>

Otherwise:

<memory>
- Visible priority: tempo this week, deferred twice now
- HRV dip tracks the short-sleep nights, not the training load
</memory>
