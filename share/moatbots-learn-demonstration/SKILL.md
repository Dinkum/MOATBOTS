---
name: moatbots-learn-demonstration
description: Turn a recorded shared-computer demonstration into a native Hermes skill.
---

The wake payload points to a recording and extracted frames under
`/workspace/.moatbots-demonstrations/<id>/`.

Study the recording, infer the repeatable goal and decisions, and create or update one narrowly
named Hermes skill in this profile. Keep volatile screen coordinates and secrets out of it. Prefer
semantic browser or application actions. Ask the human through `human.request` when intent is
ambiguous.

Replay the learned procedure against a safe target. Do not claim success from writing instructions
alone. Call `demonstrations.verify` only after the replay, with the skill path and concrete observed
result. If safe replay needs access or approval, create a human request and stop.
