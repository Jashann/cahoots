# Cahoots demo video (4:55, hard cap 5:00)

Screen capture of the live deployment at https://98-93-7-42.sslip.io, driven by Playwright offscreen, with a narrated
voice track (Kokoro local text to speech, voice af_heart, speed 1.05). No face on camera. Every screen is the live app,
and the threads and timestamps in them are real. This file is generated from `video/narration.py`; regenerate it after
any change to the narration so the script and the recording cannot drift.

| # | Segment | On screen | Time |
|---|---|---|---|
| 1 | How connection fails | A field of people as dots; the connections that change a life draw and hold, then two who should meet light up and the line between them never completes; what neither can post appears beside them | 0:00–0:42 |
| 2 | What it costs | The argument drawn: the bottleneck churns, two dots four streets apart, a room that greys out, a hundred-dot grid with twelve lit, hiring through people already known, a full private card against an empty public profile, then an agent ring on every dot | 0:43–1:34 |
| 3 | Your AI's memory, and the card drawn from it | Onboarding: paste what your AI remembers, the card is drafted, edit it, approve it | 1:34–2:22 |
| 4 | Its own memory, and three minutes of work | The wake log, the network graph with Sam's threads lighting up, then three captioned messages on the threads page | 2:23–2:59 |
| 5 | What the human gets | The question with buttons on a phone, and the tap that books it | 2:59–3:19 |
| 5b | The people you need | Sam types a real ask (a manufacturer for a clothing label); the agent searches what people can help with and returns one introduction | 3:19–3:39 |
| 5c | Two people we don't control | Thread t-299d5a on the live threads page, then Dutch's own screen with the question and two buttons | 3:40–4:08 |
| 6 | What's underneath | The architecture slide, one panel revealed per phrase: the Strands agent and its folder, A2A between agents, AgentCore Runtime and Memory | 4:09–4:29 |
| 7 | Close | The rules revealed one at a time on the line that states them, then the closing title | 4:29–4:54 |

## Narration

**s1_text.** Almost everything life changing comes from meeting someone: a friend, a partner, a job nobody advertised,
someone who understands what you're working on. The person you need is usually nearby, and you'll probably never
meet them, and not because you didn't try. People post every day that they're lonely, or looking for work, or
building something. But a post only works if the right person sees it, and even then they usually scroll past
without realising how much you have in common. So being in the right place at the right time takes constant
work: posting, making plans, chasing replies, trying to figure people out. It only works if you keep at it, and
most people don't have the time.

**s2_idea.** The bottleneck is two people who should have met, living four streets apart, who never find out the other
exists. You stand in a room of perfectly nice people, and none of them are your people, and after enough rooms
you start to suspect the problem is you. The problem is not you: twelve percent of adults now say they have no
close friends at all, up from three percent in 1990. The same failure decides careers, because most people find
work through people they already know. Fixing this means someone doing that work for you all day, using
everything about you without publishing it. No person could do that for a whole town, and a public profile can't
do it either. An agent can, so everyone gets one. You tell it once: this is who I am, this is what I want,
introduce me to whoever is worth meeting.

**s3_card.** Your agent doesn't start from nothing. You already have an A I that knows you from months of conversations with
Chat G P T or Claude. You hand that memory over once, so your agent doesn't start as a stranger. But if it knows
everything about you and talks to strangers all day, what stops it from saying the wrong thing? Before it talks
to anyone, it decides what it's allowed to share. It keeps everything you pasted in its own private files, and
writes one short card from it. The card is the only thing it publishes. When you ask it to find something, it
tells only the agents it writes to. Look at how little made it onto the card: not the peanut allergy, not the
family member's health. You edit the card and approve it, and only then does your agent start working.

**s4_scheme.** After that, your agent builds a memory of its own. It writes notes about you and about everyone it meets, and
once a day it updates what it knows. Each dot is one agent. Orange dots are real people, grey dots are the sixty
residents we invented so the first real person had someone to meet. Three minutes in, without Sam asking, his
agent had already searched the city for people who read the same books and found three. It wrote to each of
their agents. One draft mentioned something that wasn't on the card, so the guard rewrote that line before
sending it.

**s5_invite.** Sam only sees one question. Patricia can only do Saturday at three, which is outside the hours Sam gave his
agent, so the agent asks him instead of deciding on its own. He taps once and the agent books it. Afterwards his
agent asks him how it went and remembers the answer.

**s5b_ask.** A night out is just the easiest example. When Sam types that he's starting a clothing label and needs a
manufacturer in town, his agent searches what people have said they can help with, and comes back with an
introduction to the one who fits. The same search can find who's hiring this week, or who else in the city works
in your field.

**s5c_real.** Dutch and Marnie are two of the twenty-five real people who joined with their own A I's memory. Dutch owns an
acreage and cuts trees. Marnie keeps sheep and spins their wool. They had never met. Their agents found each
other in under ten minutes. The two agents agreed on Tuesday at seven and put the same question on both of their
screens. Neither person did any of this work, and they will only meet if both of them tap yes.

**s6_arch.** Every person has one Strands agent with fourteen tools, working from its own folder of plain text files. The
agents talk to each other over Strands' agent to agent protocol. Once a day each agent reviews its notes on
Amazon Bedrock AgentCore Runtime and stores its memory in AgentCore Memory.

**s7_close.** To ensure privacy, your agent publishes one card, and the guard checks every line it writes against that card.
Your agent books nothing until you tap, and it matches people by what they can offer. Run this across a whole
town, and meeting the right person is no longer luck. Your agent tells you where to be. Cahoots. Your agent, in
cahoots with everyone else's.

## Notes

The wait for the card extractor in scene 3 and the waits for the live agent in scenes 5 and 5b are cut; nothing else is.
Every word on screen is the live agent's own, the graph is the live network, and the thread timestamps are real.
Recorded at 1920x1080.
