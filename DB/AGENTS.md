# Schedule repository

Read `DB/SCHEDULE.md`, current `DB/events.json`, and `DB/travel.json` when updating schedules. Treat the JSON as the current source of truth; the notes retain the original semester rules and may predate later requests. `DB/plan.json` stores study plans. Preserve `DB/travel_reference.json`: it contains historical route details absent from the runtime travel data.

The mobile request worker is documented in `src/README.md`. It updates remote `main` directly with atomic commits and never edits this working folder. Fetch current remote changes before schedule edits, preserve local work, and never force-push. Re-read the current schedule before resolving a concurrent change.

Keep unrelated events, IDs, series, tentative status, midnight end times (`e=1440`), and cancellation/biweekly exceptions. Ask only for missing facts that matter (e.g. which occurrence, AM/PM, or the end of a new recurring series). Do not invent locations or travel times. Explain overlaps without silently moving appointments.

Schedule updates requested by the owner include validation, commit, and push so the phone calendar receives them. For bridge jobs, the worker owns validation and publishing; the invoked planning model returns JSON only.

Never put tokens, request bodies, or private result messages in this public repository. `DB/CLAUDE.md` is a user-owned local file; do not stage it. `DB/applied/` records prevent duplicate processing; preserve them. Older `.bridge/applied/` records have been moved here, with backward-compatible reads in the worker.

Validate bridge changes with `node --test src/tests/schedule.test.cjs src/tests/bridge-client.test.cjs` and `python -B -m unittest discover -s src/tests -p 'test_bridge*.py' -v`. Install updated PC worker code with `src/bridge/install.ps1`; publishing website files does not update the copied worker runtime. Pages publishes only frontend files and the three runtime JSON files using `.github/workflows/pages.yml` and `src/build.py`; the public URL remains unchanged.
