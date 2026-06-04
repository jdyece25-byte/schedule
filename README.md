# 동영 스케줄 앱

GitHub Pages로 호스팅하는 인터랙티브 일정 관리 앱.

## 배포 방법 (최초 1회)

### 1. GitHub 저장소 생성
1. [github.com/new](https://github.com/new) 접속
2. Repository name: `schedule`
3. **Public** 선택 (Pages 무료 사용)
4. Create repository

### 2. 파일 업로드
이 폴더의 파일들을 모두 업로드:
- `index.html`
- `events.json`
- `travel.json`
- `travel_db.json`
- `README.md`

```bash
# 터미널 사용 시
cd C:\Users\ASL\Desktop\Projects\schedule
git init
git add .
git commit -m "Initial schedule app"
git remote add origin https://github.com/jdyece25-byte/schedule.git
git push -u origin main
```

### 3. GitHub Pages 활성화
1. 저장소 → Settings → Pages
2. Source: **Deploy from a branch**
3. Branch: `main` / `/ (root)`
4. Save

약 1분 후 `https://jdyece25-byte.github.io/schedule/` 접속 가능.

---

## API 키 설정 (앱 내에서)

앱 접속 후 **편집 탭 → 설정** 섹션:

### GitHub PAT (저장 권한)
1. [github.com/settings/tokens](https://github.com/settings/tokens) → Generate new token (classic)
2. Note: `schedule-app`
3. Scope: **repo** 체크
4. Generate → 토큰 복사 (`ghp_...`)
5. 앱 설정에 붙여넣기

### Gemini API Key (AI 파싱)
1. [aistudio.google.com](https://aistudio.google.com) 접속 (Google 계정)
2. Get API key → Create API key
3. 키 복사 (`AIza...`)
4. 앱 설정에 붙여넣기

> 두 키 모두 브라우저 localStorage에만 저장됩니다. 파일/서버에 기록되지 않아요.

---

## 사용 방법

### 자연어로 일정 추가 (Gemini AI)
편집 탭 → 자연어로 추가 → 입력 예시:
```
정현우 6/28 토 17시-20시 잠실 파크리오 대면 과외
잠실 파크리오 → 강남역 옐로스톤 25분 지하철
```

### 직접 폼으로 추가
편집 탭 → 이벤트 직접 추가 / 이동시간 추가

### 데이터 파일 직접 수정
GitHub 웹 UI에서 `events.json` 또는 `travel.json` 클릭 → 연필 아이콘(Edit) → 수정 → Commit

---

## 파일 구조

| 파일 | 역할 |
|---|---|
| `index.html` | 메인 앱 (달력 + 공부 플랜 + 충돌 + 편집) |
| `events.json` | 이벤트 데이터 (앱이 읽고 씀) |
| `travel.json` | 장소 간 이동시간 (앱이 읽고 씀) |
| `travel_db.json` | 이동시간 상세 데이터 (참고용) |
| `schedule.html` | 구버전 (백업) |
