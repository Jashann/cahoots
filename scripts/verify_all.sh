#!/bin/bash
# Pre-submission checks. Exit non-zero on anything missing. Run from the repo root: bash scripts/verify_all.sh
set -u
cd "$(dirname "$0")/.."
fail=0; ok(){ echo "  ok   $1"; }; bad(){ echo "  FAIL $1"; fail=1; }
BASE=${CAHOOTS_LIVE:-https://98-93-7-42.sslip.io}
echo "[files]"
for f in README.md LICENSE SUBMISSION.md requirements.txt agentcore_app.py .bedrock_agentcore.yaml docs/architecture.png docs/VIDEO_SCRIPT.md data/venues.json data/personas.json cahoots/social.py cahoots/wire.py cahoots/agentfs.py; do
  [ -s "$f" ] && ok "$f" || bad "$f missing"; done
grep -q "MIT License" LICENSE && ok "MIT license text" || bad "LICENSE is not MIT"
echo "[code]"
.venv/bin/python -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('cahoots/*.py')+glob.glob('web/*.py')+['agentcore_app.py']]" && ok "python parses" || bad "syntax error"
CAHOOTS_WORK_DIR=/tmp/cahoots-verify-$$ PYTHONPATH=. .venv/bin/python -m pytest -q tests >/dev/null 2>&1 && ok "unit tests" || bad "unit tests"
echo "[deploy]"
grep -q "agent_arn: arn:aws:bedrock-agentcore" .bedrock_agentcore.yaml && ok "AgentCore runtime configured" || bad "no AgentCore ARN"
if [ -f work/admin_token.txt ]; then  # the Runtime actually ran a reflection for the demo account in the last three days (not just configured)
  TOK=$(cat work/admin_token.txt); DEMO=${CAHOOTS_DEMO_PID:-u-23bfc4}
  curl -s --max-time 20 -X POST -d "token=$TOK&pid=$DEMO" "$BASE/admin/folder" | PYTHONPATH=. .venv/bin/python -c '
import json,sys,datetime
d=json.load(sys.stdin); w=d.get("talks/_wakes.md","")
cut=(datetime.datetime.now()-datetime.timedelta(days=3)).strftime("%Y-%m-%d")
ok=[l for l in w.splitlines() if "reflect on agentcore-runtime" in l and "started" not in l and l[1:11]>=cut]
sys.exit(0 if ok else 1)' && ok "AgentCore Runtime ran a reflection for the demo account in the last 3 days" || bad "no completed Runtime reflection for the demo account in the last 3 days"
fi
for p in / /agents /network /net/registry; do code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE$p" || true); [ "$code" = "200" ] && ok "live $p" || bad "live $p returned $code"; done
curl -s --max-time 10 "$BASE/" | grep -q 'name="memory"' && ok "live homepage has the onboarding form" || bad "homepage form missing"
live=$(curl -s --max-time 10 "$BASE/version" | .venv/bin/python -c "import json,sys; print(json.load(sys.stdin).get('commit',''))" 2>/dev/null); head=$(git rev-parse HEAD)
if [ "$live" = "$head" ]; then ok "live site runs HEAD ($head)"
elif [ -n "$live" ] && git cat-file -e "$live" 2>/dev/null && ! git diff --name-only "$live" HEAD | grep -qvE '^(docs/|README\.md|SUBMISSION\.md|video/|scripts/|tests/|work/)'; then ok "live site runs $live; only docs/tests changed since (HEAD $head)"
else bad "live site runs ${live:-unknown}, HEAD is $head (deploy)"; fi
echo "[video]"
if [ -s docs/cahoots-demo.mp4 ]; then d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 docs/cahoots-demo.mp4); echo "  video ${d}s"; .venv/bin/python -c "import sys; sys.exit(0 if float('$d')<=300 else 1)" && ok "under 5:00" || bad "over 5:00"; else bad "docs/cahoots-demo.mp4 missing"; fi
echo "[repo]"
git status --porcelain | grep -q . && bad "uncommitted changes" || ok "working tree clean"
exit $fail
