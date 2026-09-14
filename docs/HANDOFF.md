# Handoff (internal, not part of the Devpost text)

## The steps only Jashan can do (in order)

1. **Watch `docs/cahoots-demo.mp4`** (3:40). If a beat is wrong, say which; scenes re-record in
   about a minute each (`video/record.py --only s4_scheme`, then `video/mux.py`).
2. **Upload it to YouTube** (public or unlisted-public), title "Cahoots: your agent is in cahoots with
   everyone else's (Agents for Humans)"; add `docs/cahoots-demo.srt` as the subtitle track. Paste the URL
   under Links above.
3. **Push the repo**: `git push --force origin main` from this folder (force, because the history was
   rewritten locally to drop a 55 MB build artifact and the video; SSH as Jashann is configured). Until
   this is done, github.com/Jashann/cahoots is an 18-file scaffold with none of the agent code: a judge
   who clicks the repo link before the push sees nothing that the README describes. Then on
   github.com/Jashann/cahoots → About (gear) → tick "MIT license" shows; add the description and the
   live demo URL.
4. **Publish the three builder.aws.com posts** from `docs/builder-posts/` (sign in with the Builder
   ID). Keep "Agents for Humans" in each title. Optional but worth up to +0.6 on a 5-point score.
5. **Fill the Devpost form** at agentsforhumans.devpost.com → Edit project: paste the sections above,
   the repo URL, the YouTube URL, the live demo URL, and the Builder ID email. Track: Everyday Agents.
   Submit early; revisions are allowed until Sep 14, 5:00 PM PDT (7:00 PM CDT).
6. **Turn on email delivery (5 minutes, optional but worth it)**: in the AWS console → Amazon SES → Identities →
   Create identity → your email address; click the verification link SES sends you; then on the EC2 box add
   `CAHOOTS_SES_FROM=<that address>` to `/etc/cahoots.env` and `docker restart cahoots`. Invites are then emailed
   to anyone who gives an address on their card (the account already has SES production access).
7. **After the deadline**: leave the EC2 instance (`i-0b825896c6ec6dbba`, ~$17/month) and the AgentCore
   runtime up through judging (ends Oct 8). Winners announced Oct 14. The $200/month budget cap is armed.

Costs so far are a few dollars of Bedrock usage; the budget page in the console shows the live number.

7. **Your own screen.** Your first-build account (`u-1f47b5`) had no key; the app now mints one at startup. Read it on the box: `sudo cat /opt/cahoots-work/users/u-1f47b5.json` (field `edit_key`), then open `https://98-93-7-42.sslip.io/me/u-1f47b5?k=<that key>`.
