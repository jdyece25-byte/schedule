# Source code

Read `../DB/AGENTS.md` for shared schedule preservation, concurrency, privacy, and verification rules. Paths in that document are relative to the repository root.

Keep frontend and worker source in `src/`, schedule data in `DB/`. Build the website with `python src/build.py <output-directory>` rather than publishing the repository tree. Keep the existing public URL and browser storage keys when changing the layout. Never recreate root-level schedule JSON files in Git; compatibility aliases are generated only in the deployment output.
