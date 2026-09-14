# Agents for Humans: what it actually takes to make Strands agents talk to each other over A2A

*builder.aws.com post 2 of 3.*

Cahoots (post 1) has one agent per person, and the agents negotiate nights out. For that to be
more than a Python loop calling methods, the agents have to be reachable as agents: addressable,
discoverable, talking a protocol. Strands ships A2A on both sides. Here's what worked and what bit.

## Serving a person's agent

```python
from strands.multiagent.a2a import A2AServer

server = A2AServer(
    agent_factory=lambda ctx: person_agent(profile, venues),   # a fresh Agent per A2A context
    http_url=f"{base}/agents/{profile.person_id}",             # what other agents will call
    serve_at_root=True,                                        # I do the mounting
    enable_a2a_compliant_streaming=True,
)
root_app.mount(f"/agents/{profile.person_id}", server.to_fastapi_app())
```

Three things I learned the hard way:

1. **The agent needs a `description`.** `A2AServer` refuses to publish an agent card without one.
   Obvious in hindsight; the error is clear.
2. **Mount paths double up unless you say `serve_at_root=True`.** `to_fastapi_app()` derives a
   mount path from `http_url`, so mounting that app under the same path gives you
   `/agents/p000/agents/p000/...` and a 404 on the agent card.
3. **One process, many agents is fine.** Sixty `A2AServer`s mounted on one FastAPI app share a
   port; each has its own agent card at `/agents/{id}/.well-known/agent-card.json`.

## Talking to it

`strands-agents-tools` has an `A2AClientToolProvider`. Give a Strands agent its tools and it can
discover and message other agents; or use its client directly for a deterministic exchange:

```python
provider = A2AClientToolProvider(known_agent_urls=[url])
r = asyncio.run(provider._send_message(proposal_text, url))
```

## The reply is not where you think

An A2A task response carries the whole exchange: your message, the agent's message, artifacts.
My first parser joined every text part it could find and "parsed" my own proposal back as the
agent's decision. The fix is to read artifacts first, then history entries with `role == "agent"`,
and never the echoed user turn. Also: streamed text arrives as chunks; join with `""`, not `" "`,
or `<thinking>` blocks from some models leak through your regex.

## Dates

Models misread ISO timestamps across the wire more often than you'd expect ("this outing is on a
Monday" for a Saturday). Every proposal now carries the day and time in words, `Saturday 2:00 PM`,
next to the timestamp, with an instruction to trust the words.

## Where it runs

Locally the whole city negotiates in-process for speed; the agents the web app serves negotiate
over A2A, and the transcript marks each exchange "over A2A" so you can see which is which. The
deployed shape is the same code on Amazon Bedrock AgentCore, where each person's agent could run
in its own runtime and still be reachable by URL.

Next post: the hook that makes "only the card is shared" true in code, not just in the prompt.

Repo: https://github.com/Jashann/cahoots
