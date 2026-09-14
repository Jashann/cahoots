# Scale, and the second city

An agent never talks to a whole city. It searches the directory first (a keyword index here; an
embedding index in production), talks to a shortlist, and remembers the rest in files. Cost grows
with the number of plans, not the number of users, because agents only talk when there is something
to decide. Agents are independent services addressed by URL; sharding by city and running them as
pooled workers is the production shape.

Cold start is the honest objection: a network app with nobody in it is nothing. Winnipeg here started
with sixty seeded residents; every real person who joins makes the seed matter less. A second city
starts the same way, with a seed drawn from real public venues and interests, replaced by real people
as they arrive. The problem is not small: in the 2021 American Perspectives Survey, 12% of adults said
they have no close friends, up from 3% in 1990, and the most common reason people give for not seeing
friends is the work of arranging it.
