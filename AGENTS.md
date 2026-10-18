# Schedule project

Read `DB/AGENTS.md` before editing schedules or changing service state. Code is in `src/`, database files are in `DB/`, and the running request service is installed independently in `%LOCALAPPDATA%/ScheduleBridge`. DB-only and frontend-only changes never require stopping or reinstalling that service. Keep it running, reconcile concurrent Git updates, and check real service health before completing the task.

For tablet/browser cloud tasks, read `TABLET_SETUP.txt`. Repository access permits editing repository files, not accessing the owner's Windows installation. Follow the cloud-specific health-check limits in `DB/AGENTS.md` and distinguish a local change, GitHub publication, Pages deployment, and installed PC runtime updates.
