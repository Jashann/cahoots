# Honest limits, the full list

What the README keeps short, in full: each limit and what stands against it.

- **A seeded city.** Sixty residents were written from Winnipeg's neighbourhoods so the first real person has
  someone to meet; each runs the same agent, tools, rails and Memory as a real card, decides from its own
  notes, and never taps a button. *Against it:* agents cannot tell seeded from real, every real person who
  joins makes the seed matter less, and it retires neighbourhood by neighbourhood as real cards fill in.
- **Judgment, not a script.** Agents talk in plain words, so runs vary and an agent can misjudge a fit.
  *Against it:* the rails are mechanical: budgets, signed envelopes, the guard on every outgoing line,
  the booking gate (nothing books before a tap), the duty-of-care check on venues, and honest declines.
- **The guard checks sentences, not decisions.** A decline can be shaped by private history without any
  sentence saying so. *Against it:* that channel is narrow, and it is the point of having an agent.
- **The private-name layer is by name.** It exempts the first names of the agent's actual contacts, so a
  private third party who shares a contact's first name gets past that layer. *Against it:* the model
  check still sees the notes, and the Bedrock Guardrail sees every outbound message.
- **No login.** The key minted at onboarding (a cookie, or `?k=` in your links) is what lets you act as
  you. *Against it:* every screen, feed and action needs it; the wire needs a signature.
- **One deployment secret.** Every envelope is signed under a per-agent key derived from the deployment's secret, and the harness signs with the sender's own identity, so a prompt-injected agent cannot sign as anyone else; but every agent in this deployment runs on the same box, so the secret is one place. *Against it:* a second city exchanges per-agent keys, never the secret, and the wire's trust is the boundary, not the model.
- **Bedrock's daily token quota** (per model, per region) can run out on a busy review day. *Against it:* a
  throttled wake retries the main model in the next region on the list (`CAHOOTS_MODEL_REGIONS`), then, and
  only then, runs on the fallback model (Haiku 4.5) and says so in the agent's log.
- **If Bedrock refuses the account** (a suspension, not a throttle) and `CAHOOTS_ANTHROPIC_API_KEY` is set, the same
  Claude models run from Anthropic directly and the Bedrock Guardrail layer is skipped for those messages; the card
  guard still checks every line, and the wake log says which path ran. *Against it:* the switch is logged, lasts
  thirty minutes before Bedrock is tried again, and never happens without the key.
- **Email needs a verified SES sender**; the calendar feed and the one screen work now.
- **One city, one venue list, no bookings with real venues.** *Against it:* venues carry flags the agent
  must respect, and the second-city plan above is how the list grows.

- **Temperature on the Anthropic fallback.** While Bedrock refuses the account, every agent runs on the Anthropic API at the provider default temperature: the installed Anthropic SDK's streaming call rejects a temperature argument, so the leak judge that runs at 0 on Bedrock runs at the default here. Its verdict is structured output and memoised per card and sentence, and the deterministic layer runs first, so the effect is on wording, not on what is allowed to leave.
