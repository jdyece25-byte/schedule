# Schedule repository

Read `SCHEDULE.md`, current `events.json`, and `travel.json` when updating schedules. Treat the JSON as the current source of truth; the notes retain the original semester rules and may predate later requests.

The mobile request worker is documented in `bridge/README.md`. It updates remote `main` directly with atomic commits and never edits this working folder. Fetch current remote changes before schedule edits, preserve local work, and never force-push. Re-read the current schedule before resolving a concurrent change.

Keep unrelated events, IDs, series, tentative status, midnight end times (`e=1440`), and cancellation/biweekly exceptions. Ask only for missing facts that matter (e.g. which occurrence, AM/PM, or the end of a new recurring series). Do not invent locations or travel times. Explain overlaps without silently moving appointments.

Schedule updates requested by the owner include validation, commit, and push so the phone calendar receives them. For bridge jobs, the worker owns validation and publishing; the invoked planning model returns JSON only.

Never put tokens, request bodies, or private result messages in this public repository. `CLAUDE.md` is a user-owned local file; do not stage it as part of unrelated work. `.bridge/applied/` records prevent duplicate processing; preserve them.

Validate bridge changes with `node --test tests/schedule.test.cjs tests/bridge-client.test.cjs` and `python -m unittest discover -s tests -p 'test_bridge*.py' -v`. Install updated PC worker code with `bridge/install.ps1`; publishing website files does not update the copied worker runtime.
