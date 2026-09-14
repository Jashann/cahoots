# Agents for Humans: I gave everyone in a city an agent, and the agents plan the nights out

*builder.aws.com post 1 of 3. Publish before Sep 14, 5 PM PDT. Title must contain "Agents for Humans".*

"We should hang out soon" is the text that never becomes a plan. For the AWS Agents for Humans
hackathon I built Cahoots: every person gets a Strands agent, the agents talk to each other, and
each person receives one message a week: "Saturday 8 PM, Across the Board Game Café, 6 people,
Priya writes novels and Marcus reads the same sci-fi you do. In?"

This post is about the shape of the thing. Two follow-ups cover the A2A plumbing and the consent
hook.

## The design in one paragraph

The human does three things, ever: approve a card, tap In or Not this one, and answer "How was
it?" Everything else is agents. One agent per person (a Strands `Agent` with two tools and a
system prompt that carries the person's card). Each week a few agents play host and pitch an
outing at a real venue. The others decide for their humans with a structured reply (accept,
decline, or counter, with a one-sentence reason and a 0-10 fit). The host can revise once. Groups
that reach their minimum become invites, each drafted by the guest's own agent in words that guest
would care about.

## Why agents, and where they are not allowed

The interesting question in any "agentic" project is what a rules engine can't do. Here:

- Is this person a fit for *these* people? A quiet novelist and a hairstylist who lives for
  karaoke can both like food, and still shouldn't be at the same table.
- Is the counter-proposal worth changing the plan for?
- How do you phrase an invite to this specific human?

Those are judgment calls, and they're the agent's. What a rules engine *can* do (is the person
free at 7, is the venue over budget, do they hate loud places) runs first as a deterministic
ranker, so the models spend tokens on judgment, not arithmetic. Splitting the two made the system
cheaper and better at the same time.

## Structured output everywhere

Every decision is a Pydantic model, and Strands' `structured_output_model` makes that one line:

```python
reply = self._agent(prompt, structured_output_model=ProposalReply).structured_output
# ProposalReply(decision="counter", reason="Friday doesn't work, Saturday would", fit=7, counter={...})
```

Cards, host pitches, replies and invites are all typed. It means the transcript of what the agents
said to each other is machine-readable, and it means a bad reply fails loudly instead of quietly.

## Cold start, honestly

A network with nobody in it is nothing. I generated 60 residents of Winnipeg with Amazon Nova and
Claude on Bedrock (a retired teacher who runs a book club, an immigrant accountant who cooks and
plays cricket), gave each an agent, and let real users join the same pool. The demo says so out
loud. The mechanism is the point; the population comes later.

Next post: serving every person's agent over the A2A protocol so they can live on different
machines and still scheme together.

Repo: https://github.com/Jashann/cahoots
