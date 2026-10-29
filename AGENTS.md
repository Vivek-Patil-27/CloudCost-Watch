# CloudCost Watch

Full project context, status, decisions and remaining work are in HANDOVER.md. Read it all before doing anything.

Open and read HANDOVER.md now.

Most important rules (details in HANDOVER.md section 14):
- User is a beginner on Windows: one small step at a time, exact commands, use `py` not `python`.
- Back up app.py to backups/ before editing; run `py -m unittest -v test_app.py` after every change.
- Never call simulated CPU/memory data "live"; never claim a feature works without testing it.
- After app changes, rebuild cloudcost_deploy.zip and give the PythonAnywhere upload steps (HANDOVER.md section 7).
