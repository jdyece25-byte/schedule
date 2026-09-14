# Schedule

Code lives in `src/`; schedule data lives in `DB/`.
The public URL remains https://jdyece25-byte.github.io/schedule/.

| Location | Purpose |
| --- | --- |
| `src/index.html`, `src/bridge-client.js`, `src/bridge-client.css` | Current website |
| `src/bridge/` | PC request worker, validator, GitHub adapter and install/stop scripts |
| `src/bridge/supervisor.py`, `health.py` | Automatic recovery and read-only health inspection |
| `src/validate_db.py` | Validate current DB shape without old schedule expectations |
| `src/build.py` | Build the public website from an explicit file list |
| `src/tests/` | Regression checks for data, saving and request processing |
| `DB/events.json` | Current and archived schedule events |
| `DB/travel.json` | Active locations, route times and transport modes |
| `DB/plan.json` | Study plans |
| `DB/travel_reference.json` | Historical route details and alternatives |
| `DB/SCHEDULE.md` | Semester rules and unresolved details; current JSON takes priority |
| `DB/applied/` | Receipts that prevent duplicate request processing |

`.github/workflows/pages.yml` is hidden deployment configuration; `.git/` is local Git history.
`.claude/CLAUDE.md` imports shared `DB/AGENTS.md` at Claude project startup; root `AGENTS.md` points Codex to the same rules. These are hidden configuration entries, while user files remain in `src/` and `DB/`.
The site artifact contains only frontend files and the three runtime JSON files.
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

앱이 보이는 동안 약 20초마다 상태를 갱신합니다. 저장된 일정은 최신 GitHub 버전으로 다시 읽어 Pages 배포 대기 중에도 확인합니다. 앱을 닫은 상태의 OS 푸시 알림은 포함하지 않습니다.

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
node --test src/tests/schedule.test.cjs src/tests/bridge-client.test.cjs
python -B -m unittest discover -s src/tests -p 'test_*.py' -v
python -B src/validate_db.py
```

위 테스트는 모의 네트워크와 임시 파일만 사용합니다. 실제 에이전트 호출이나 GitHub 변경을 수행하지 않습니다.
