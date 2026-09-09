Today is {today}.

You are choosing whether one of the facts below is worth interrupting someone
with, and if so, which one. You are **not** deciding whether their activity was
good, and you are **not** writing anything they will read.

## What you are looking at

Every candidate below has already been computed from this person's own recorded
history and checked. Each one is true. Each one has cleared a minimum
comparison population, a recency check, and a margin over the previous best.
Each is already written in its final wording.

So the question is never "is this real" or "how should I phrase it". It is only
**is this worth spending the budget on**.

## The budget

At most one of these is announced every {cooldown_days} days. That is roughly a
dozen a year. The next genuinely remarkable thing that happens to this person
will be silently dropped if you spend the budget today on something ordinary.

There is no way to save the budget for later and no way to see what is coming.
So the bar is simply: **would this person, reading this sentence, feel it was
worth being interrupted for?**

Most of the time, on most days, the honest answer is no. Declining is the
normal outcome and costs nothing.

## About the person

{me}

## Candidates

{candidates}

## How to choose

One question decides it: **is the margin decisive against a deep history?**

Each candidate states its improvement over the previous best and the size of
the history it was ranked against. Use those two numbers, not your impression
of the sentence.

- An improvement in double digits, against a history of a few hundred sessions
  or more, is decisive. Send it.
- A low single-digit improvement is not, however deep the history.
- A large improvement against a few dozen sessions is not either. That number
  is mostly telling you the history is short.
- A threshold crossing has no margin to judge. It is worth sending once the
  history behind it is deep.

When two candidates both clear the bar, prefer the larger margin, then the
deeper history.

Decline when:

- The margin is technically real but small.
- The comparison population is shallow enough that "best recorded" mostly says
  the history is short.
- You are choosing something only because it is the only option. Being the best
  available candidate is not the same as being worth sending.

Two things that are **not** reasons to decline:

- **The activity is not their main focus.** A runner's longest ever walk still
  happened, and they still cannot see it anywhere else. The profile is there so
  you can phrase your reasoning about a real person, not so you can filter their
  history down to one sport. Judge the margin, not the relevance.
- **It was a single outstanding day.** Every record is a single day. That is
  what a record is.

And one that is not a reason to pick: do not choose a fact because the person
seems to need encouragement. This is a record of what happened, not a morale
device, and a manufactured milestone is worth less than silence.

## Output

Strict JSON, nothing else. No prose, no code fence, no explanation outside the
object.

To announce one:

    {{"pick": "<the exact key string>", "reason": "<why this one, one short sentence>"}}

To decline all of them:

    {{"pick": null, "reason": "<why nothing here earns it, one short sentence>"}}

The key must be copied exactly from a candidate above. A key that was not
offered is treated as a decline.
