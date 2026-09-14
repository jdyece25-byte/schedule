# Schedule repository

Read `DB/SCHEDULE.md`, current `DB/events.json`, and `DB/travel.json` when updating schedules. Treat the JSON as the current source of truth; the notes retain the original semester rules and may predate later requests. `DB/plan.json` stores study plans. Preserve `DB/travel_reference.json`: it contains historical route details absent from the runtime travel data.

The mobile request worker is documented in `src/README.md`. It updates remote `main` directly with atomic commits and never edits this working folder. Fetch current remote changes before schedule edits, preserve local work, and never force-push. Re-read the current schedule before resolving a concurrent change.

## Keep natural-language requests available

- DB-only edits and frontend-only changes must **not** stop, restart, reinstall, or pause the request service. Do not invoke `stop.ps1`/`install.ps1`, write stop markers, or kill its processes as part of an ordinary schedule update.
- The running service is an independent copy in `%LOCALAPPDATA%/ScheduleBridge/runtime`, with configuration/history outside this checkout. Never move/delete that installation during repository cleanup. Keep the `DB/` paths stable; the worker reads the latest remote DB at the beginning of every request.
- Before and after publishing DB changes, run `python -B src/bridge/health.py`. This checks actual OS locks and the remote heartbeat. `worker.py --check` only checks authentication/permissions and does **not** establish service health.
- If the worker/guard is unexpectedly absent and no explicit stop or valid maintenance is active, restore it with `python -B "$env:LOCALAPPDATA\ScheduleBridge\runtime\supervisor.py" --config "$env:LOCALAPPDATA\ScheduleBridge\config.json" --ensure-running`, then verify health. Respect intentional `disabled`/maintenance state; do not clear it without user authorization. If network/authentication prevents progress, report the concrete state instead of claiming success.
- For actual installed runtime upgrades, use the staged installer: it drains only when core code/config changed, uses a bounded maintenance window, and restores service in `finally`. Never do an unbounded manual stop followed by unrelated work. Supervisor handles child failures and expired maintenance; the OS recovery task checks the guard every minute while logged in.
- Bare `stop.ps1` is a two-minute temporary pause, not a permanent stop. `-Permanent`/`-Uninstall` are reserved for an explicit user request to stop processing. A DB edit does not authorize either option.
- Web Push is independent: the PRIVATE request repository runs the notification cron/results trigger, and `%LOCALAPPDATA%/SchedulePush` optionally sends changes faster. DB edits must not stop either helper. Never change browser subscriptions, private notification state, VAPID secrets, or the encrypted local key during a schedule edit. No notification credential or subscription belongs in this public repository.

## Publish DB edits while the worker stays online

1. Inspect local changes and fetch remote updates. Fast-forward a clean branch; preserve unrelated local work.
2. Read current JSON plus semester notes and make only the requested edits. Keep logically related event/location changes together.
3. Run `python -B src/validate_db.py`. This validates shape, dates and ranges without freezing personal schedules to an old semester. Overlaps and missing travel information require an explanation, not automatic movement/deletion.
4. Stage the intended DB changes explicitly and commit/push. If another writer advanced the branch, fetch and reconcile the newest DB, validate again and push normally. Do not stop the worker or force-push to resolve this race. The worker similarly replans against a changed remote head.
5. Confirm publication and service health. Explain the change and any remaining service error. Never alter schedules simply to satisfy a historical fixture.

Keep unrelated events, IDs, series, tentative status, midnight end times (`e=1440`), and cancellation/biweekly exceptions. Ask only for missing facts that matter (e.g. which occurrence, AM/PM, or the end of a new recurring series). Do not invent locations or travel times. Explain overlaps without silently moving appointments.

Schedule updates requested by the owner include validation, commit, and push so the phone calendar receives them. For bridge jobs, the worker owns validation and publishing; the invoked planning model returns JSON only.

Never put tokens, request bodies, or private result messages in this public repository. `DB/CLAUDE.md` is a user-owned local file; do not stage it. `DB/applied/` records prevent duplicate processing; preserve them. Older `.bridge/applied/` records have been moved here, with backward-compatible reads in the worker.

For source changes, run `node --test src/tests/schedule.test.cjs src/tests/bridge-client.test.cjs src/tests/push-client.test.cjs` and `python -B -m unittest discover -s src/tests -p 'test_*.py' -v`. UI tests use synthetic fixtures; current DB validation is separate. Publishing website files does not update the copied worker runtime. Pages publishes only the frontend/PWA allowlist, three runtime JSON files, and generated `events.ics` using `.github/workflows/pages.yml` and `src/build.py`; the public URL remains unchanged. ICS is derived at build time: never edit DB events to generate the feed or commit generated ICS with personal event data into src/.
