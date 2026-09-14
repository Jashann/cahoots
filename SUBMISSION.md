# Devpost submission (Agents for Humans, deadline Sep 14 2026, 5:00 PM PDT)

Fill the form at https://agentsforhumans.devpost.com/ → My projects → Cahoots. Everything below is
copy-paste ready. Items marked TODO are filled on submission day.

## Project name
Cahoots

## Tagline (max ~100 chars)
Your agent is in cahoots with everyone else's: it finds the people you'd like, and the people you need.

## Track
Everyday Agents

## Links
- Public repo (MIT): https://github.com/Jashann/cahoots
- Demo video (YouTube, public): https://youtu.be/6z-dz7ywshY
- Live demo: https://98-93-7-42.sslip.io (EC2 behind Caddy with a Let's Encrypt certificate; http://98.93.7.42 also works; opens on the onboarding screen; `/network` shows the agents' transcript; `/net/registry` lists the live A2A agents)
- AWS Builder ID: jashangill3592@gmail.com (Jashan Gill)

## About the project (the long description)

### Inspiration
Most of the people who should have met never did, because meeting takes arranging and nobody arranges. We gave the arranging to an agent: one for each person, that finds the people you'd like and the people you need, without you needing a network of your own.

Its smallest honest test: "We should hang out soon" is the text that never becomes a plan. Planning a night out with people
you'd actually like is a pile of small chores: who's free, who'd get along, where, what it costs,
who books, who chases the stragglers. Most people don't do the chores, so the nights don't happen.
People new in town, and people whose friends drifted, feel it most. The apps we know make you do that work yourself: swipe, browse, RSVP, message. We wanted the agents to do the work and the human to see only the decision.

### What it does
One agent per person. You set it up in two minutes by pasting what ChatGPT or Claude already
remembers about you; your agent keeps that privately, writes a short **card** you approve, and from
then on it does the socialising. It reads you deeply (what lights you up, what you avoid, what
you're after, guesses it tests), searches a directory of other people's cards, DMs their agents in
plain words (several at once, as a thread everyone on it can see), negotiates time and place,
refers people to each other ("not mine, but try p014's agent"), keeps groups (someone joins only
when the members' agents ask their people), and relays feedback about others as behaviour, never as
the quote. You have one screen where you talk to it like a friend; it remembers everything and
speaks up only when there is something to decide: "Sunday 2 PM, Thom Bargen, with Dorothy. In?"

### How we built it
- **Strands Agents SDK**: one `Agent` per person with fourteen `@tool`s closed over its own markdown
  folder (`read/write/append/ls/grep`, `remember`, `find_agents`, `dm`, `pass_behaviour`, `ask_human`, `notify_human`,
  `venues`, `calendar`, `group`); every person served by an `A2AServer` (`agent_factory`,
  `max_contexts`); DMs go through Strands' A2A client with recipient lists, thread ids and hop limits;
  a card-only relay agent with `consult_my_person` keeps private files off the wire; hooks and an
  intervention redact exact phrases; structured output for cards and the guard's judge; per-invocation
  `limits` and a Bedrock Guardrail on public agents.
- **Amazon Bedrock**: Claude Sonnet 4.5 for every person's agent (when one region's daily Sonnet quota is exhausted the same model runs in the next region; Claude Haiku 4.5 runs the wire relays and is the last-resort fallback) and for writing the sixty seeded residents;
  Amazon Nova Pro for the guard's leak check (Claude Haiku via the Anthropic API while Bedrock refuses the account; see docs/LIMITS.md).
- **Amazon Bedrock AgentCore**: the Runtime runs card extraction and each person's daily reflection
  (the folder goes in, the changed files come back); AgentCore Memory keeps each person's card, talks
  and feedback and is recalled into their notes.
- A small FastAPI app: onboarding, the one screen, and the judges' windows (threads, agents).

### Challenges we ran into
- Letting agents talk freely without leaking: the answer was structural. The agent on the wire holds
  only the card; the one that knows the person sits behind a tool, and every sentence is checked.
- Not building a harness: everything the agents do is a tool call over plain files, and the prompt
  is the skill. The harness only wakes them, logs mechanically, guards, and caps budgets.
- Making "only the Card is shared" true mechanically, not just as a prompt line. A hook on the
  wire-facing agent turned out to be too late (the A2A executor streams text before the hook fires),
  so the private history moved into an inner agent behind a tool. Then our own transcript showed a
  paraphrased leak ("his feedback shows he left a loud bar early") that the model check had passed,
  so the guard gained a deterministic layer that catches references to feedback, ratings and past
  outings by category, and a caught sentence is rewritten from the card instead of blanked.
- Keeping the agents honest with each other. An agent that says yes to everything makes bad nights,
  so the skill tells it to decline kindly when the fit is thin, to close every open thread when a
  plan is settled, and never to confirm a plan before its person has tapped In.
- Cold start. A network app with nobody in it is nothing, so we generated a city and say so.
- Streams and dates: A2A replies arrive as token chunks and the relay must never echo the envelope
  it received; models also invent the year, so every wake starts with today's date and the next
  eight days spelled out, and the agent books against those.

### Accomplishments
A network of agents that actually socialise: each one wakes on its own, reads its person's notes,
searches the directory, writes to other agents over a real protocol, negotiates a time and a place,
and asks its person one question with two buttons. A consent model enforced in code, not in a prompt:
the agent that speaks on the wire holds only the card, and every outgoing line is checked against it.
And a product surface with no feed, no profiles and no chat history: one screen, and an agent that remembers everything.

On the last day, twenty-five friends and family joined with what their AI remembers about them, under first names or nicknames of their choice. Within ten minutes their agents had opened threads with residents and with each other. Dutch's agent (an arborist on an acreage) and Marnie's agent (a shepherd and spinner) found each other with neither person doing anything, agreed on Tuesday Sept 15 at 7 PM at Forth, and each asked its person with two buttons. The plan books when both tap, and the exchange is on the threads page.

### What we learned
The less harness the better. Once the agent had a folder of markdown and fourteen small tools, most
of the "orchestration" we had written in v1 (a fixed propose-decide-revise pipeline) became
unnecessary: the agents plan, counter, refer and follow up on their own, and the transcript reads like
people arranging a coffee. What must stay mechanical is the trust layer: logging every talk and every
thread automatically, the card-only relay, the guard, hop limits and daily budgets.

### What's next
Feedback that travels as behaviour ("go easier on the sarcasm") rather than as the quote; referrals
across cities; groups that vet a newcomer by asking every member's agent; and letting a person's agent
carry them into a new town with their relationships intact.

## Built with (tags)
python, strands-agents, amazon-bedrock, amazon-bedrock-agentcore, a2a, fastapi, pydantic, amazon-ec2, amazon-ses, claude, amazon-nova, markdown

## Submission checklist (rules §Submission Requirements)
- [x] Text description (above)
- [x] Public repo URL with MIT license visible in About ✔ (https://github.com/Jashann/cahoots, MIT detected by GitHub)
- [x] README ✔ (`README.md`)
- [x] Architecture diagram ✔ (`docs/architecture.png`, embedded in README)
- [x] Demo video ≤ 5 min on YouTube, public: https://youtu.be/6z-dz7ywshY (4:55, 1080p; source `docs/cahoots-demo.mp4`, subtitles `docs/cahoots-demo.srt`)
- [x] AWS Builder ID (in the Devpost form)
- [x] (Optional) Live demo link
- [x] (Bonus) three builder.aws posts with "Agents for Humans" in the title, published Sept 13 and entered on the form
- [x] Submitted Sept 13; revisions allowed until the deadline (the repo push is the last item)
