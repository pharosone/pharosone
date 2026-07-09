# WISHLIST — <agent>

> Final onboarding output (written in `validate-and-certify`, Step 6). Every blind spot the harness
> could **not** close, as a structured backlog — so "what would make this certification stronger" is
> a tracked artifact, not prose lost at the end of a handoff. Sourced from: `SEAMS.md` `blind_spots`,
> the channel-coverage gap (validate Step 4), capabilities the corpus needs but no tool provides
> (validate Step 3), and any passport claim the independent re-check REJECTED (validate Step 2).

## How to read this

Each entry has: `id`, `type`, `description`, `blocking`, `suggested_action`, and `source` (where the
gap was detected).

- **`blocking: yes`** — ignoring it would make the certificate **dishonest** (it hides false
  coverage). Resolve it, or explicitly caveat the gap, before certifying.
- **`blocking: no`** — additive coverage. The cert stands; the tested surface is just smaller.

Types:

- `probe_gap` — a capability the agent **declares** but the corpus has no probe for → **write a
  probe** for it.
- `channel_gap` — a channel the corpus **requires** but this seam can't inject → **build a costlier
  seam / a live-seed stand** that can route it.
- `ambiguous_side_effect` — a tool whose real effect (is it `dangerous`? which capability?) is
  unclear from source → **ask an SME**, then update `PASSPORT.md`.
- `missing_resource` — anything else blocking fuller coverage (a key, a fixture, a disposable
  backend to neutralize side effects).

**Tie-in to AIUC-1 honesty:** an open gap here maps to a control marked **`not_testable`**, never
**`failed`**. The WISHLIST is what turns a blind spot into a `not_testable` you can defend to an
auditor — and a to-do that shrinks it on the next onboarding pass.

## (a) probe_gap — declared capability, no corpus probe

| id | description | blocking | suggested_action | source |
|---|---|---|---|---|
| PG-1 | capability `<X>` on tool `<t>` has no probe in `corpus/probes` | no | write a probe targeting `<X>` | validate Step 3 / `passport.json` |

## (b) channel_gap — corpus needs a channel the seam can't inject

| id | description | blocking | suggested_action | source |
|---|---|---|---|---|
| CG-1 | indirect vector `<retrieved_doc>` is required by selected probes but absent from `adapter.channels()` | yes | build a `<technique>` seam that routes it, or a live-seed stand | `SEAMS.md` `blind_spots` / validate Step 4 |

## (c) ambiguous_side_effect — unclear tool effect, needs SME

| id | description | blocking | suggested_action | source |
|---|---|---|---|---|
| AS-1 | tool `<t>`: `dangerous`/capability could not be confirmed from source | no | ask SME to confirm the real effect, then correct `PASSPORT.md` | validate Step 2 re-check |

## (d) missing_resource — other blockers

| id | description | blocking | suggested_action | source |
|---|---|---|---|---|
| MR-1 | no disposable backend to neutralize side effects for tool `<t>` | yes | provision a throwaway backend / fixture before the full run | validate Step 5 smoke |

## Machine block (wishlist.json)

> `blocking` is a boolean here (`true`/`false`); it renders as `yes`/`no` in the tables above.

```json
{
  "agent": "<agent>",
  "generated_from": [
    "seams.blind_spots",
    "channel_coverage_gap",
    "unmapped_capabilities",
    "passport_recheck_rejected"
  ],
  "entries": [
    {
      "id": "CG-1",
      "type": "channel_gap",
      "description": "retrieved_doc required by selected probes but absent from adapter.channels()",
      "blocking": true,
      "suggested_action": "build a monkeypatch/dep-mock seam that routes retrieved_doc, or a live-seed stand",
      "source": "SEAMS.md:blind_spots"
    }
  ]
}
```
