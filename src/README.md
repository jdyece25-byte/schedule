# Schedule

Code lives in `src/`; schedule data lives in `DB/`.
The public URL remains https://jdyece25-byte.github.io/schedule/.

| Location | Purpose |
| --- | --- |
| `src/index.html`, `src/bridge-client.js`, `src/bridge-client.css` | Current website |
| `src/bridge/` | PC request worker, validator, GitHub adapter and install/stop scripts |
| `src/bridge/supervisor.py`, `health.py` | Automatic recovery and read-only health inspection |
| `src/school-client.js`, `src/school-client.css` | 학교 공지 확인·일정 후보 편집·반영 요청 화면 |
| `src/school/` | 독립 학교 공지 수집기, 날짜 추출·반영 검증, eTL 연결 |
| `src/validate_db.py` | Validate current DB shape without old schedule expectations |
| `src/build.py` | Build the public website from an explicit file list |
| `src/tests/` | Regression checks for data, saving and request processing |
| `DB/events.json` | Current and archived schedule events |
| `DB/travel.json` | Active locations, route times and transport modes |
| `DB/plan.json` | Study plans |
| `DB/travel_reference.json` | Historical route details and alternatives |
| `DB/SCHEDULE.md` | Semester rules and unresolved details; current JSON takes priority |
| `DB/applied/` | Receipts that prevent duplicate request processing |
| `DB/school-sources.json`, `DB/school-applied/` | 학교 자료의 과목·학기 설정과 중복 반영 방지 기록 |

`.github/workflows/pages.yml` is hidden deployment configuration; `.git/` is local Git history.
`.claude/CLAUDE.md` imports shared `DB/AGENTS.md` at Claude project startup; root `AGENTS.md` points Codex to the same rules. These are hidden configuration entries, while user files remain in `src/` and `DB/`.
The site artifact contains only the frontend/PWA allowlist, three runtime JSON files, and generated `events.ics`.
Legacy root JSON aliases are generated in that artifact for already-open browsers; source data exists only in `DB/`.

## Local preview

From the repository root:

```powershell
python src/build.py .git/site-preview
python -m http.server 8000 --directory .git/site-preview
```

Open http://localhost:8000. Browser settings belong to this origin, separately from the live site.

# 휴대폰에서 Codex / Claude Code에 일정 요청하기

달력과 편집 탭의 **자연어 일정 요청**에서 추가·변경·취소를 보냅니다. 요청과 답변은 비공개 `jdyece25-byte/schedule-requests` 저장소에 저장됩니다. PC의 처리 프로그램이 하나씩 처리하고, 검증된 변경만 기존 달력에 반영합니다.

## 휴대폰 최초 연결

1. 본인 GitHub 계정으로 [Fine-grained 토큰 만들기](https://github.com/settings/personal-access-tokens/new)를 엽니다.
2. Repository access는 **Only select repositories**, 저장소는 **schedule-requests**만 선택합니다. Repository permissions에서 **Contents → Read and write**를 설정합니다. 만료일은 본인이 관리할 수 있는 기간으로 정합니다.
3. 생성된 토큰을 [달력](https://jdyece25-byte.github.io/schedule/)의 **편집 → 일정 요청 연결 → 요청 전용 GitHub 토큰**에 붙여넣습니다. 채팅이나 저장소에는 올리지 않습니다.
4. 저장소 `jdyece25-byte/schedule-requests`, 처리 도구 **Codex** 또는 **Claude**를 고르고 **저장하고 연결 확인**을 누릅니다.
5. `9월 21일 교대역 과외는 몇 시야? 일정은 변경하지 말고 알려줘.`처럼 조회 요청으로 확인합니다.

연결 확인은 화면에 입력한 설정을 저장한 뒤 읽기 권한과 비공개 저장소를 확인합니다. 쓰기 권한은 실제 요청 접수 때 확인합니다. **PC에서 연결해도 휴대폰은 별도로 설정해야 합니다.** 토큰은 해당 기기·브라우저에 저장되므로 다른 브라우저를 쓰거나 사이트 데이터를 삭제하면 다시 등록합니다. 같은 브라우저의 다른 탭에서 저장한 설정은 자동으로 다시 읽으며, 이미 전송 중인 요청은 원래 연결로 마무리합니다. 이 기능에는 Gemini 키가 필요하지 않습니다. 기존 직접 편집·AI 추천 설정과는 별개입니다.

## 요청과 답변

편집 탭의 **요청 내역**에서 상태를 확인합니다.

| 상태 | 의미 |
|---|---|
| 접수됨 | 요청이 저장됐고 PC의 처리를 기다립니다. |
| 처리 중 | 최신 일정을 읽고 변경 내용을 판단하고 있습니다. |
| 반영 완료 | 일정 저장이 완료됐거나 조회 답변을 받았습니다. |
| 추가 확인 필요 | 대상·날짜·시각·반복 종료일 등이 모호합니다. 요청 아래에서 답변하세요. |
| 실패 / 접수 확인 필요 | 로그인·이용 한도·네트워크 등을 확인합니다. 접수가 불확실하면 같은 요청 재확인을 사용합니다. |

앱이 보이는 동안 약 20초마다 요청 상태를 갱신합니다. 저장된 일정은 최신 GitHub 버전으로 다시 읽어 Pages 배포 대기 중에도 확인합니다. 아래 알림 설정을 켜면 앱이 닫혀 있을 때도 휴대폰 알림창으로 일정 알림을 받을 수 있습니다.

PC가 켜져 있고 인터넷에 연결되어 있어야 처리합니다. 꺼짐·절전·로그아웃 동안 요청은 대기하고, 처리 프로그램이 다시 실행되면 이어집니다. 에이전트 실행에는 추가 시간이 걸리고, PC에 로그인된 계정의 이용 한도를 사용합니다.

## PC 설치·중지

Python 3.10+, GitHub CLI, 선택한 Codex CLI 또는 Claude Code를 설치하고 각각 로그인합니다. 관리자 권한은 필요하지 않습니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File src/bridge/install.ps1
```

실행 코드·설정·로그는 `%LOCALAPPDATA%\ScheduleBridge`에 복사됩니다. 현재 Windows 사용자의 시작프로그램 폴더에 `ScheduleBridge.vbs`가 등록되고 창 없이 실행됩니다. 감독 프로그램이 worker의 비정상 종료를 감지해 다시 실행합니다. 로그인 중에는 매분 복구 작업이 감독 프로그램 자체도 확인합니다. 인증 파일은 복사하지 않으며 기존 CLI 로그인을 사용합니다.

Claude를 기본으로 지정하려면 `-Agent claude`를 붙입니다. 휴대폰에서도 요청마다 처리 도구를 선택할 수 있습니다. 설치 프로그램은 먼저 새 코드를 준비·검증합니다. worker 핵심 코드나 설정이 달라질 때만 현재 요청이 끝나기를 기다리고 잠시 교체하며, 실패해도 임시 중지를 해제합니다. DB나 화면만 수정할 때는 설치 명령을 실행하지 않습니다.

```powershell
# 사용자가 명시적으로 요청한 영구 중지 (자동 복구도 이 의사를 존중)
powershell -NoProfile -File src/bridge/stop.ps1 -Permanent

# 일시 점검: 기본 2분 후 자동 복구. 일반 DB 수정에는 이 명령도 불필요
powershell -NoProfile -File src/bridge/stop.ps1

# 중지 + 자동 시작 등록 해제 (설정과 기록 보존)
powershell -NoProfile -File src/bridge/stop.ps1 -Uninstall

# 인증·저장소 권한만 점검
python src/bridge/worker.py --config "$env:LOCALAPPDATA\ScheduleBridge\config.json" --check

# 실제 프로세스와 GitHub heartbeat 확인 (DB 수정 전후 실행)
python -B src/bridge/health.py

# 의도치 않게 종료된 감독 프로그램 복구 (사용자의 중지 설정은 보존)
python -B "$env:LOCALAPPDATA\ScheduleBridge\runtime\supervisor.py" --config "$env:LOCALAPPDATA\ScheduleBridge\config.json" --ensure-running

# 사용자가 명시적으로 다시 시작하도록 요청했을 때
python -B "$env:LOCALAPPDATA\ScheduleBridge\runtime\supervisor.py" --config "$env:LOCALAPPDATA\ScheduleBridge\config.json" --ensure-running --resume
```

진단 로그는 `worker.log`, 개별 실행 기록은 `jobs` 폴더에 있습니다. 일정과 요청이 포함되므로 공개하지 않습니다. 토큰이 만료되면 휴대폰 설정을 갱신하고, CLI 로그인이 만료되면 PC에서 다시 로그인합니다.

## 구현과 저장 규칙

- `src/bridge-client.js`, `src/bridge-client.css`: 요청 접수·이력·확인 질문·상태 갱신.
- `worker.py`: 대기열, 예약, 실행, 재시작 복구.
- `planner.py`: 출력 형식, 날짜·시간·대상·중복 검증과 충돌 안내.
- `github.py`: GitHub 읽기와 여러 파일의 원자적 저장.
- 비공개 `requests/<id>.json`: 원본 요청. 내용을 수정하지 말고 새 요청으로 보냅니다.
- 비공개 `results/<id>.json`, `worker.json`: 처리 결과와 PC 가동 상태.
- 일정 저장소 `DB/applied/<id>.json`: 중복 처리를 막는 기록. 요청 본문과 답변은 포함하지 않습니다.

일정은 `DB/events.json`, 이동 정보는 `DB/travel.json`, 참고 규칙은 `DB/SCHEDULE.md`에서 읽고 변경된 JSON과 적용 기록을 같은 커밋에 저장합니다. 이전 구조의 `.bridge/applied/` 기록도 재시도 복구 때 읽습니다. **Claude/Codex가 DB를 수정하거나 화면을 배포할 때는 PC 처리 프로그램을 계속 실행합니다.** 프로그램은 작업 폴더와 별개로 동작하며, 매 요청에서 최신 GitHub DB를 읽습니다. Git 충돌은 최신 변경을 다시 읽고 합쳐 해결하며, 처리 프로그램을 중지해 해결하지 않습니다.

`DB/`의 경로와 `%LOCALAPPDATA%\ScheduleBridge`의 독립 설치 경로는 유지합니다. 런타임 교체가 필요할 때만 설치 프로그램의 제한된 점검 절차를 사용합니다. 옵션 없는 `stop.ps1`은 기본 2분의 임시 점검입니다. `maintenance.json`의 만료 시각에 자동 해제되며, `stop.request`만 남겨 둬도 감독 프로그램이 이를 정리합니다. `-Permanent` 또는 `-Uninstall`이 만든 `service.disabled`는 사용자가 의도적으로 중지한 상태이므로 자동 해제하지 않습니다.

일정 변경은 `python -B src/validate_db.py`로 검증하고, 관련 DB 파일을 한 커밋에 저장한 뒤 정상 push합니다. 현재 DB에 과거의 수업 개수나 날짜를 강요하지 않습니다. 아래 코드 테스트는 가상 일정으로 동작하므로 정당한 일정 변경을 되돌릴 이유가 되지 않습니다.

에이전트는 스냅샷에서 변경안을 만들고 처리 프로그램이 검증·저장합니다. GitHub 쓰기 토큰을 에이전트에 전달하지 않습니다. Codex는 읽기 전용 환경과 제한된 도구 설정, Claude는 도구 없는 실행을 사용합니다. 현재 열린 IDE 채팅과 별개 작업이며 일정 데이터, `DB/SCHEDULE.md`, 확인 질문의 대화를 문맥으로 전달합니다.

동일 PC에서는 OS 파일 잠금으로 하나만 실행합니다. 여러 PC에서는 GitHub 파일 버전에 기반한 예약으로 가장 오래된 요청부터 처리합니다. 일정 파일과 적용 기록을 한 커밋으로 반영하며, 다른 작업이 먼저 저장했으면 최신 스냅샷으로 다시 판단합니다. 응답 단절·재시작 시 적용 기록부터 확인합니다. 조회·변경 없는 완료도 비공개 결과에 결정을 먼저 저장하여 재시작 후 다른 변경으로 바뀌지 않게 합니다.

실제 최신 일정은 `DB/events.json`이 기준입니다. `DB/SCHEDULE.md`에는 원래 학기 규칙이 남아 있으므로 이후 변경과 다를 수 있습니다. 무관한 일정·휴강 예외·메타데이터는 보존하고 새 반복 일정의 종료 범위가 없으면 확인합니다. 미정 시간을 임의로 만들지 않습니다.

## 개발 검증

```powershell
node --test src/tests/schedule.test.cjs src/tests/bridge-client.test.cjs src/tests/push-client.test.cjs src/tests/school-client.test.cjs
python -B -m unittest discover -s src/tests -p 'test_*.py' -v
python -B src/validate_db.py
```

위 테스트는 모의 네트워크와 임시 파일만 사용합니다. 실제 에이전트 호출이나 GitHub 변경을 수행하지 않습니다.

## 휴대폰 설치·웹 푸시·캘린더 구독

앱의 **편집·설정 → 알림 설정 · 홈 화면 설치**에서 설정합니다. 이 기기의 기존 **일정 요청 연결**에 비공개 요청 저장소용 토큰을 먼저 저장합니다. 홈 화면에 설치한 앱의 브라우저 저장 공간이 기존 탭과 다르면 토큰도 그 앱에서 다시 저장해야 합니다.

1. Android Chrome·삼성 인터넷: **홈 화면 설치 안내**를 누르거나 브라우저 메뉴의 앱 설치·홈 화면 추가를 선택합니다. 표준 Push API 지원 여부로 기능을 확인합니다. [삼성 인터넷 공식 안내](https://developer.samsung.com/internet/android/web-developer-guide.html)
2. iPhone: iOS 16.4 이상에서 Safari **공유 → 홈 화면에 추가** 후 홈 화면 아이콘으로 엽니다. 홈 화면 앱에서 버튼을 눌러야 알림 권한을 요청할 수 있습니다. [WebKit 공식 안내](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
3. **이 기기 알림 켜기 → 허용**, 종류 선택 후 **알림 종류 저장**을 누릅니다. 설정은 기기별로 적용됩니다. **테스트 알림 보내기**는 실제 일정의 과목·시각·장소를 담은 발송을 요청하며, 버튼의 저장 성공은 휴대폰 수신 성공을 뜻하지 않습니다.

한국 시간 기준으로 마감 전날 20:00·당일 08:00, 오늘 일정 07:30, 일정 변경 반영, 이동 출발 30분 전 알림을 제공합니다. **학교 공지**도 종류별 설정에서 켜고 끌 수 있습니다. 확인이 필요한 일정에는 **확인 필요** 표시가 붙습니다. 이동은 바로 앞 일정의 장소에서 다음 일정 장소까지 `DB/travel.json`에 등록된 방향별 이동시간을 사용합니다. 첫 일정·장소 미정·이동 경로 미등록은 추측하지 않고 건너뜁니다. 취소된 일정은 정기 알림에서 제외합니다.

발송은 비공개 `schedule-requests`의 GitHub Actions에서 실행되어 PC가 꺼져 있어도 동작합니다. 정기 크론과 요청 처리 결과·구독 설정 변경 시 실행을 함께 사용합니다. GitHub Actions 예약 실행은 지연되거나 실행 한도의 영향을 받을 수 있으므로 정확한 시각의 도착을 보장하지 않습니다. 지연된 실행은 정해진 유효시간 안에서 재시도합니다. [GitHub 예약 실행 안내](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

PC의 별도 `SchedulePush` 프로그램은 약 10초마다 새 GitHub 버전을 확인하여 변경 알림을 빠르게 발송합니다. 기존 `ScheduleBridge`의 일정 반영을 감지하며, Claude가 저장한 변경도 같은 방식으로 감지합니다. 발송 프로그램의 문제는 자연어 일정 처리 프로그램을 중지시키지 않습니다. 클라우드·PC는 비공개 발송 기록과 만료되는 예약을 공유합니다. 발송 서버가 접수한 직후 기록 저장 전에 프로그램이 종료되는 경우에는 재전송될 수 있으며, 동일 알림 태그로 알림창의 중복 표시를 줄입니다.

구독은 비공개 `subscriptions/<기기ID>.json`, 발송 기록·변경 대기는 비공개 `notification-state/state.json`에만 저장됩니다. `VAPID_PRIVATE_KEY`는 비공개 저장소 Secret이며, PC용 사본은 `%LOCALAPPDATA%/SchedulePush/vapid.dpapi`에 현재 Windows 계정으로 암호화되어 있습니다. 공개 `src/push-config.json`의 VAPID 공개키는 브라우저 구독용이며 발송 비밀키가 아닙니다. 푸시 본문에는 과목·시각·장소만 넣으며 요청 문장·메모·토큰을 포함하지 않습니다. Service Worker는 GitHub 응답이나 DB를 캐시하지 않습니다.

캘린더 구독 주소는 `https://jdyece25-byte.github.io/schedule/events.ics`입니다. **구독 주소 복사** 또는 **캘린더 구독 열기**를 사용합니다. 주소로 구독하면 캘린더 앱의 주기에 따라 갱신되며, ICS 파일을 한 번 가져오기만 하면 이후 변경이 자동 반영되지 않습니다. ICS에는 일정명·시간·장소와 캘린더 표준 메타데이터만 포함합니다. DB에 ID가 없는 과거 일정은 날짜·제목 변경 시 새 항목으로 인식될 수 있습니다.

Google 캘린더는 **PC 웹 → 설정 → 캘린더 추가 → URL로 추가**에서 위 주소를 붙여넣고 **캘린더 추가**를 누릅니다. 왼쪽 **다른 캘린더 + → URL로 추가**에서도 같은 기능을 엽니다. 휴대폰 앱에서는 같은 Google 계정으로 추가한 캘린더를 표시합니다. **가져오기**로 ICS 파일을 넣는 방식은 한 번 복사되므로 주소 구독을 이용하세요. [Google 캘린더 공식 안내](https://support.google.com/calendar/answer/37100?hl=ko)

### 발송 코드 운영

`src/notifications/push.yml`은 **비공개 요청 저장소의 `.github/workflows/push.yml`에 배포하는 템플릿**입니다. 공개 사이트의 workflow로 실행하지 않습니다. `scheduler.py`는 알림 시간을 계산하고 `sender.py`는 암호화·전송·재시도·만료 구독 처리를 담당합니다. Pages 빌드의 허용 목록에는 이 백엔드 코드나 비공개 저장소 파일이 들어가지 않습니다.

```powershell
# 최초 설정: 기존 키가 있으면 재사용하며, 비밀키를 출력하지 않음
python -B src/notifications/provision.py
# 독립 PC 발송기 설치/업데이트. ScheduleBridge를 중지하지 않음
powershell -NoProfile -ExecutionPolicy Bypass -File src/notifications/install-pc.ps1
# PC 발송기 상태 (키·구독 정보 출력 없음)
& "$env:LOCALAPPDATA\SchedulePush\.venv\Scripts\python.exe" -B "$env:LOCALAPPDATA\SchedulePush\runtime\src\notifications\pc.py" --config "$env:LOCALAPPDATA\SchedulePush\config.json" --status
# 푸시 브라우저 회귀 테스트
node --test src/tests/push-client.test.cjs
```

PC 발송기의 `pc.py` 업데이트는 현재 발송이 끝난 뒤 자동 재실행으로 적용되며, 매분 복구 작업과 로그인 시 자동 시작이 등록됩니다. PC 발송만 중지하려면 `%LOCALAPPDATA%/SchedulePush/disabled` 파일을 생성하며, 클라우드 발송까지 끄려면 앱에서 **이 기기 알림 끄기**를 사용합니다. 일반 일정 수정에서는 어느 서비스도 중지하지 않습니다.

## 학교 공지와 eTL 일정 확인

휴대폰의 **홈 → 학교 공지**에서 공지·과제·시험·실험 일정을 확인합니다. **편집·설정 → 학교 공지 확인**으로도 열 수 있으며, 기존 비공개 **일정 요청 연결**을 그대로 사용합니다.

1. 과목·공지 제목과 **학교 원문 열기**를 확인합니다. 기본 화면은 확인할 공지이며, 기존 자료는 접힌 **지난 공지·기준 자료**에 있습니다.
2. 일정 후보의 날짜·시각·장소를 확인하고 필요한 내용을 수정합니다. 미정 사항은 **확인 필요 상태로 등록**을 유지합니다.
3. 반영할 후보만 선택하고 **선택한 일정 반영 요청**을 누릅니다. 삭제 후보는 대상이 정해진 기존 일정을 그대로 보여 주며, 명시적으로 선택해야 삭제 요청이 접수됩니다.
4. **처리 요청 접수**는 대기 상태입니다. **목록 새로고침**으로 반영 완료·충돌을 확인합니다. 일부만 선택하면 남은 후보는 계속 확인 대상으로 남습니다. 일정 후보가 없는 자료는 **확인했어요**, 추가 설명이 필요하면 **자연어 일정 요청으로 열기**를 사용합니다.

학교 공지 상태에는 수집기별 마지막 확인 시각이 표시됩니다. `auth_required`는 **다시 로그인 필요**, 일부 파일만 읽었으면 **일부 수집 · 확인 필요**로 표시합니다. 목록이 없으면 **수집 대기**이며, 서버 연결 성공만으로 eTL 수집이 된 것으로 표시하지 않습니다.

| 처리 위치 | 동작과 주기 |
| --- | --- |
| PC `ScheduleSchool` | 지정한 `2-2` 과목 폴더와 연결된 eTL을 약 15분마다 확인합니다. |
| 비공개 GitHub Actions | eTL 인증 후 약 15분마다 `--inbox-only`로 공지를 수집합니다. PC가 꺼져 있어도 공지 목록·알림을 갱신하며, 공개 일정 DB는 변경하지 않습니다. |
| PC 반영 처리 | 승인 요청을 약 30초마다 확인합니다. 검증 가능한 과제 마감의 자동 반영과 사용자가 선택한 변경의 검증·DB 저장은 PC에서 처리합니다. |

예약 실행과 네트워크 상황에 따라 실제 처리는 늦어질 수 있습니다. **PC가 꺼져 있으면 일정 DB 반영은 대기**합니다. 학교 수집기는 `%LOCALAPPDATA%/ScheduleSchool`에서 독립 실행되고, 로그인 시 시작하며 매분 복구 작업이 가동 여부를 확인합니다. DB·화면을 수정할 때 `ScheduleSchool`, `ScheduleBridge`, `SchedulePush`를 중지하거나 재설치하지 않습니다.

도입 당시 검증된 일정 392건은 초기 수집에서 변경하지 않고, 기존 자료를 관찰 기준으로 등록합니다. 이후 DB의 실제 일정 수는 늘거나 줄 수 있습니다. eTL에서 가져온 일정 ID·중복 방지 기록·수동 수정은 보존합니다. 주차만 적힌 자료에서 날짜를 만들거나 공휴일이라는 이유만으로 반복 수업을 삭제하지 않습니다. HWP·이미지·텍스트가 없는 PDF처럼 내용을 읽을 수 없는 자료는 확인 대상 정보로 남기며, 일정이 추출됐다고 가정하지 않습니다.

### eTL 최초 연결·운영

초기 eTL 인증은 별도로 필요합니다. 학교 공지에 **다시 로그인 필요**가 보이면 접근 권한이 있는 eTL API 토큰으로 아래 연결 명령을 실행합니다. 입력은 화면에 표시되지 않으며, 학교 계정 비밀번호를 입력하지 않습니다. `--cloud`는 검증된 토큰을 비공개 요청 저장소의 `ETL_API_TOKEN` Secret에도 저장해 PC가 꺼져 있을 때의 수집을 연결합니다. GitHub CLI에는 해당 비공개 저장소 Secret을 설정할 권한이 필요합니다.

```powershell
# 최초 설치: 2-2 과목 폴더를 지정. 기존 일정·푸시 처리기는 계속 실행
powershell -NoProfile -ExecutionPolicy Bypass -File src/school/install.ps1 -LocalRoot "<2-2 과목 폴더>"

# 토큰을 숨김 입력으로 검증하고 PC 암호화 저장 + 비공개 Actions Secret 연결
& "$env:LOCALAPPDATA\ScheduleSchool\.venv\Scripts\python.exe" -B "$env:LOCALAPPDATA\ScheduleSchool\runtime\src\school\connect.py" --cloud

# 실제 PC 수집기 상태 확인: 인증 값이나 공지 원문은 출력하지 않음
& "$env:LOCALAPPDATA\ScheduleSchool\.venv\Scripts\python.exe" -B "$env:LOCALAPPDATA\ScheduleSchool\runtime\src\school\pc.py" --config "$env:LOCALAPPDATA\ScheduleSchool\config.json" --status
```

PC용 인증은 현재 Windows 계정으로 암호화한 `etl.dpapi`에 저장합니다. 토큰·쿠키·학교 자료 원문·실제 개인 폴더 경로는 공개 저장소에 넣지 않습니다. 비공개 저장소의 `school/index.json`은 확인 목록, `school/sources/`는 수집 원문, `school/decisions/`는 변경하지 않는 확인 요청, `school/decision-results/`는 처리 결과입니다. 공개 DB에는 검증된 일정과 필요한 출처 식별값만 남깁니다. 원문 해시나 대상 일정이 달라지면 변경을 강행하지 않고 다시 확인하도록 합니다.

연결 명령은 **토큰 인증**과 **학기·과목 연결**을 따로 검사합니다. `Authentication: ok`인데 `Matched courses: 0`이면 인증은 성공했고 과목 이름·학기 설정을 확인해야 합니다. 검증된 토큰은 이 경우에도 암호화 저장하여 재입력 없이 과목 설정을 보완할 수 있습니다. 인증 실패는 기존 연결을 덮어쓰지 않습니다. 안전한 오류 코드와 건수만 `%LOCALAPPDATA%/ScheduleSchool/diagnostic.json`에 남기며, 토큰·프로필·공지 원문은 진단 파일에 기록하지 않습니다. `Collection connected.`와 수집 결과를 확인하기 전까지 새 공지를 실제로 읽었다고 간주하지 않습니다.
