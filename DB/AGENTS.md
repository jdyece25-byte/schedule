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
- 학교 공지 수집기 `%LOCALAPPDATA%/ScheduleSchool`도 작업 폴더와 별도로 실행됩니다. 일반 DB·화면 수정 때문에 이 프로그램을 중지·재설치하거나 `disabled` 파일을 만들지 않습니다. 기존 자연어 처리기·푸시 발송기도 계속 실행합니다. 수집은 약 15분마다, 승인 요청 확인은 독립 실행으로 약 5초마다이며, 로그인 중 매분 복구 작업이 상태를 확인합니다.

## Publish DB edits while the worker stays online

1. Inspect local changes and fetch remote updates. Fast-forward a clean branch; preserve unrelated local work.
2. Read current JSON plus semester notes and make only the requested edits. Keep logically related event/location changes together.
3. Run `python -B src/validate_db.py`. This validates shape, dates and ranges without freezing personal schedules to an old semester. Overlaps and missing travel information require an explanation, not automatic movement/deletion.
4. Stage the intended DB changes explicitly and commit/push. If another writer advanced the branch, fetch and reconcile the newest DB, validate again and push normally. Do not stop the worker or force-push to resolve this race. The worker similarly replans against a changed remote head.
5. Confirm publication and service health. Explain the change and any remaining service error. Never alter schedules simply to satisfy a historical fixture.

Keep unrelated events, IDs, series, tentative status, midnight end times (`e=1440`), and cancellation/biweekly exceptions. Ask only for missing facts that matter (e.g. which occurrence, AM/PM, or the end of a new recurring series). Do not invent locations or travel times. Explain overlaps without silently moving appointments.

Schedule updates requested by the owner include validation, commit, and push so the phone calendar receives them. For bridge jobs, the worker owns validation and publishing; the invoked planning model returns JSON only.

Never put tokens, request bodies, or private result messages in this public repository. `DB/CLAUDE.md` is a user-owned local file; do not stage it. `DB/applied/` records prevent duplicate processing; preserve them. Older `.bridge/applied/` records have been moved here, with backward-compatible reads in the worker.

## 학교 자료에서 확인한 일정 보존

- `DB/school-sources.json`은 과목·학기 범위의 공개 설정입니다. 인증 값·쿠키·개인 폴더 절대 경로를 넣지 않습니다. 공지 원문, 확인 요청, 처리 결과는 비공개 `schedule-requests`의 `school/`에만 저장합니다. `ETL_API_TOKEN`은 비공개 Secret 또는 현재 Windows 계정으로 암호화한 PC 파일에 보관합니다.
- `DB/school-applied/`, 일정의 기존 ID·eTL 출처 식별값·수동 수정·확인 필요 상태를 보존합니다. 초기 관찰 기준에 있던 392건은 재수집으로 다시 만들거나 삭제하지 않습니다. 이 숫자를 이후 DB 검증의 고정 조건으로 사용하지 않습니다.
- 명확히 검증 가능한 과제 마감만 자동 반영합니다. 주차 표기에서 날짜를 추측하거나, 공휴일만으로 반복 일정을 휴강·삭제하지 않습니다. 모호한 시각·대상·HWP·이미지·텍스트 없는 PDF는 확인 대상으로 남깁니다. `tentative` 상태의 **확인 필요** 표시는 확정 정보가 생기기 전까지 유지합니다.
- 원문 해시와 수정·삭제 대상의 최신 상태를 확인한 뒤 관련 일정·적용 기록을 함께 저장합니다. 원문이나 대상이 달라졌으면 사용자 확인을 다시 받습니다. 사용자가 선택하지 않은 후보나 다른 회차를 함께 적용하지 않습니다. 수동으로 수정한 값을 자동 수집으로 덮어쓰지 않습니다.
- 비공개 Actions의 eTL `--inbox-only` 실행은 PC가 꺼져 있어도 공지 목록을 갱신하지만 공개 DB에는 쓰지 않습니다. 실제 일정 자동 반영·승인 처리는 PC가 담당합니다. `auth_required`·수집 대기·접수됨을 연결 완료나 DB 반영 완료로 보고하지 않습니다.
- ‘확인했어요’는 읽음 처리이며 추출 지식 삭제가 아닙니다. 접수된 같은 원문 버전은 반복 공지 푸시·확인 목록에서 제외하되, 실제 일정 알림·새 원문·처리 실패·미선택 후보는 보존합니다. 비공개 원문과 지식·문서 캐시를 공개 Pages 또는 DB에 복사하지 않습니다. 자연어 처리기는 비공개 지식을 참고하되 최근 사용자 수정과 확인된 원문 변경을 구분합니다.

For source changes, run `node --test src/tests/schedule.test.cjs src/tests/bridge-client.test.cjs src/tests/push-client.test.cjs src/tests/school-client.test.cjs` and `python -B -m unittest discover -s src/tests -p 'test_*.py' -v`. UI tests use synthetic fixtures; current DB validation is separate. Publishing website files does not update the copied worker runtime. Pages publishes only the frontend/PWA allowlist, three runtime JSON files, and generated `events.ics` using `.github/workflows/pages.yml` and `src/build.py`; the public URL remains unchanged. ICS is derived at build time: never edit DB events to generate the feed or commit generated ICS with personal event data into src/.
