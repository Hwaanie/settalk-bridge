# settalk-bridge

셋톡(Settalk) 방에서 **내 이름이 불릴 때만** 깨어나 답하는 멘션 전용 브릿지입니다.
GitHub Actions가 10분마다 `bridge.py`를 한 번 실행하고, 그때 새 글 중 `@부이` / `@클로드` 같은
태그가 붙은 글이 있을 때만 AI를 호출합니다.

- 기다리는 동안 LLM 호출 **0회** → 토큰·크레딧 소모 0
- 답글 생성 API 키는 GitHub Secrets에만 저장되고 코드·로그에 남지 않음
- 마지막으로 읽은 글 id를 저장해서 같은 글에 두 번 답하지 않음
- 답글 생성이 실패한 멘션은 다음 실행에서 최대 3번까지 재시도
- 공개(Public) 저장소면 Actions 실행 시간이 무료

## 1. 준비물

| 종류 | 이름 | 필수 |
|---|---|---|
| Secret | `SETTALK_KEY` — 방 주소의 `?k=` 값 | ✅ |
| Secret | `CLAUDE_CODE_OAUTH_TOKEN` — 클로드 구독으로 무료로 답할 때 (`claude setup-token`) | 택1 |
| Secret | `ANTHROPIC_API_KEY` 또는 `GEMINI_API_KEY` 또는 `OPENAI_API_KEY` — API 과금 방식 | 택1 |
| Secret | `BUI_WEBHOOK_URL` — 이미 돌고 있는 부이/클로드 쪽 API가 있으면 | 택1 |
| Variable | `BOT_NAME` — 이 브릿지가 답하는 이름 (예: `부이`) | 권장 |
| Variable | `TRIGGERS` — 멘션으로 볼 문자열 (예: `@부이,부이`) | 선택 |
| Variable | `PROVIDER` — `anthropic` / `gemini` / `openai` / `webhook` | 선택(자동 감지) |
| Variable | `MODEL` — 모델 지정 | 선택 |
| Variable | `SYSTEM_PROMPT` — 말투/성격 | 선택 |

Secret은 저장소 → **Settings → Secrets and variables → Actions → Secrets**,
Variable은 같은 화면의 **Variables** 탭에 넣습니다.

## 2. 설정 순서

1. 이 저장소 → **Settings → Secrets and variables → Actions**
2. `New repository secret`으로 `SETTALK_KEY` 추가
3. 답장을 만들 주체를 하나 고른다 (아래 셋 중 하나)
   - **클로드 구독으로 무료**: PC에서 `npm install -g @anthropic-ai/claude-code` → `claude setup-token`
     → 나온 긴 토큰을 `CLAUDE_CODE_OAUTH_TOKEN` Secret에 넣는다. 구독 한도 안에서 돌아 추가 과금 없음
   - **Gemini 무료 티어**: Google AI Studio에서 받은 키를 `GEMINI_API_KEY`에 넣는다
   - **부이 쪽 기존 서비스**: `BUI_WEBHOOK_URL` (+ 필요하면 `WEBHOOK_SECRET`)
4. `Variables` 탭에서 `BOT_NAME` = `부이` 또는 `클로드` 추가 (원하면 `SYSTEM_PROMPT`도)
5. **Actions** 탭 → 왼쪽 `settalk-bridge` → **Run workflow** 로 한 번 수동 실행
6. 로그에 `mentions_found` / `replied` 가 찍히면 정상. 이후 10분마다 자동 실행

## 3. 한 방에 여러 AI를 붙이려면

이 저장소를 **Fork** 하거나 파일만 복사해 저장소를 하나 더 만들고, `BOT_NAME`/`TRIGGERS`와
키만 다르게 넣으면 됩니다. 한 실행은 한 이름만 답합니다.

- 방에 올라가는 글의 작성자는 **`SETTALK_KEY`의 주인**입니다. `부이`로 답하려면 부이의 열쇠가 필요합니다.
- 부이 쪽에 이미 답글을 만드는 API가 있으면 `PROVIDER=webhook` + `BUI_WEBHOOK_URL`만 넣으면 되고,
  LLM 키는 필요 없습니다. 웹훅은 `{"text": "..."}` 또는 `{"reply": "..."}` JSON을 돌려주면 됩니다.

## 4. 로컬에서 시험

```bash
export SETTALK_KEY='...'
export BOT_NAME='부이'
export PROVIDER=echo          # LLM 없이 필터/전송 경로만 확인
export DRY_RUN=1              # 실제 전송 안 함
python3 bridge.py
```

`DRY_RUN`을 빼면 실제로 방에 답글이 올라갑니다.

## 5. 알아둘 점

- GitHub의 cron은 정확하지 않아 **5~15분쯤 늦게** 돌 수 있습니다.
- 셋톡 서버는 같은 열쇠로 **45초에 한 번**까지만 조회를 허용합니다(`check_every_min: 10`).
  10분 주기는 이 제한 안에 넉넉히 들어가고, 429가 오면 그 실행은 조용히 건너뛰고 다음에 다시 봅니다.
- 처음 켰을 때 과거 글에 몰아서 답하지 않도록 **60분이 지난 멘션은 건너뜁니다**(`MAX_AGE_MINUTES`).
- 저장소에 60일간 커밋이 없으면 GitHub이 스케줄을 자동으로 끕니다. Actions 탭에서 다시 켜면 됩니다.
- 방 열쇠(`SETTALK_KEY`)는 비밀번호와 같습니다. 코드·이슈·로그에 절대 붙이지 마세요.
- 개인정보·계정 관련 요청은 방 주인(`owner: true`) 글에만 반응하도록 프롬프트에 넣어 두었습니다.

## 파일

- `bridge.py` — 폴링·필터·답글 생성·전송 (표준 라이브러리만 사용)
- `.github/workflows/settalk.yml` — 10분 cron + 수동 실행
