# Agents for Humans: consent as a Strands hook, and memory that stays with the person

*builder.aws.com post 3 of 3.*

Cahoots agents represent real people to other people's agents. The first question anyone asks is
"what does my agent say about me?" The answer has to be enforceable, not promised. This post is
about the two mechanisms: a hook that redacts, and AgentCore Memory scoped per person.

## The card is the only thing shared

Onboarding pastes what ChatGPT or Claude already remembers about you. The agent reads it once and
produces a short **card** with structured output (interests, availability, what a good night looks
like, what a bad one looks like). The prompt is explicit about what to drop: health, money, work
drama, family. On my own memory export it dropped a peanut allergy and a note about a family
member's health without being told which lines were which. The human then edits the card and
approves it. The raw text is never stored.

## A hook that makes rule one mechanical

A system prompt line ("share only the card") is a promise. A hook is a guarantee. Strands hooks
let you intercept events in the agent loop; `MessageAddedEvent` fires for every message the agent
produces:

```python
@dataclass
class CardGuard(HookProvider):
    profile: PrivateProfile
    caught: list[str] = field(default_factory=list)

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(MessageAddedEvent, self.on_message)

    def on_message(self, event: MessageAddedEvent) -> None:
        msg = event.message
        if msg.get("role") != "assistant":
            return
        for block in msg.get("content", []):
            if "text" in block:
                for term in self._private_terms():          # feedback phrases, do-not-repeat list
                    if re.search(re.escape(term), block["text"], re.I):
                        self.caught.append(term)
                        block["text"] = re.sub(re.escape(term), "[withheld: not on card]", block["text"], flags=re.I)
```

The private terms are everything the person told their agent that is *not* on the card: "How was
it?" answers in their words, the people they'd rather not see again. If the model ever tries to say
"Jashan found the café too loud" to another agent, the hook rewrites it before it leaves, and the
transcript can honestly show "0 leaks this week."

## Memory that belongs to one person

AgentCore Memory does the long-term part. Each person is an `actor_id`; the memory has a
user-preference strategy and a semantic strategy, both namespaced under `/people/{actorId}/...`.
Three kinds of events go in: the approved card, invite replies, and feedback.

```python
client.create_event(memory_id=mid, actor_id=person_id, session_id=f"outing-{oid}",
                    messages=[("How was it? good, Priya was great, the café was too loud", "USER"),
                              ("Noted. I'll plan around that next week.", "ASSISTANT")])
```

A few minutes later, `retrieve_memories` on that person's namespace returns records like
*"Enjoys board-game cafés as a social venue"* and *"Prefers group sizes of 4 to 8"*. Before the
agent decides for its human, it folds the top records into its private history. Because the
namespace is per actor, one person's agent cannot retrieve another person's memories, and the
guard makes sure it cannot repeat its own human's either.

## What this buys

The pitch line is "your agent is in cahoots with everyone else's." That only works if people
trust what their agent says on their behalf. Structured cards, a redacting hook, and per-person
memory are the three pieces that make the trust checkable instead of hoped for.

Repo: https://github.com/Jashann/cahoots
