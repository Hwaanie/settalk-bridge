#!/usr/bin/env python3
"""셋톡(Settalk) 멘션 전용 브릿지.

10분마다 한 번 깨어나 방의 새 글만 확인하고,
자기 이름(@부이 / @클로드 ...)이 불린 글에만 AI로 답한다.
기다리는 동안에는 LLM 호출이 0회이므로 토큰도 0.

설계 원칙
  * 새 글 확인은 HTTP GET 한 번. 멘션이 없으면 그걸로 끝(LLM 호출 없음).
  * 답글 생성 API 키는 환경변수(GitHub Secrets)로만 받고 로그에 남기지 않는다.
  * 마지막으로 읽은 id를 상태 파일에 남겨 같은 글에 두 번 답하지 않는다.
  * 답글 생성이 실패한 멘션은 pending에 담아 다음 실행에서 한 번 더 시도한다.

환경변수
  SETTALK_KEY      (필수) 셋톡 방 열쇠. 방 주소의 ?k= 값과 같다.
  SETTALK_BASE     기본 https://chat.gonipro.app
  BOT_NAME         이 브릿지가 답하는 이름. 비우면 서버가 알려주는 me 값을 쓴다.
  TRIGGERS         멘션으로 볼 문자열들(쉼표 구분). 기본 "@<BOT_NAME>"
  PROVIDER         groq | anthropic | openai | gemini | webhook | echo  (비우면 자동 선택)
  GROQ_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / BUI_WEBHOOK_URL
  GROQ_BASE_URL    기본 https://api.groq.com/openai/v1 (OpenAI 호환 엔드포인트)
  MODEL            모델 지정(선택). 없으면 PROVIDER별 기본 후보를 차례로 시도.
  SYSTEM_PROMPT    봇 성격/말투(선택)
  MAX_REPLY_CHARS  답글 최대 길이(기본 900자)
  MAX_MENTIONS_PER_RUN  한 실행에서 처리할 최대 멘션 수(기본 5)
  CONTEXT_MESSAGES 답글 생성에 함께 넣을 앞 대화 수(기본 10)
  STATE_FILE       상태 파일 경로(기본 .state/state.json)
  SINCE_ID         상태 파일이 없을 때 시작 id(기본 0)
  DRY_RUN=1        실제 전송 없이 결과만 출력
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("SETTALK_BASE", "https://chat.gonipro.app").rstrip("/")
KEY = os.environ.get("SETTALK_KEY", "").strip()
BOT = os.environ.get("BOT_NAME", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", ".state/state.json"))
LAST_ID_FILE = Path(os.environ.get("LAST_ID_FILE", "last_id.txt"))
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() not in ("", "0", "false", "no")
MAX_MENTIONS = int(os.environ.get("MAX_MENTIONS_PER_RUN", "5") or 5)
MAX_AGE_MIN = int(os.environ.get("MAX_AGE_MINUTES", "60") or 0)
MAX_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "900") or 900)
CONTEXT_N = int(os.environ.get("CONTEXT_MESSAGES", "10") or 10)
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "25") or 25)
PENDING_MAX_ATTEMPTS = 3


def log(msg: str) -> None:
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


def http(method: str, url: str, payload=None, headers=None) -> tuple[int, str]:
    data = None
    hdrs = {"User-Agent": "settalk-bridge/1.0", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as e:
        raise HttpError(0, f"connection failed: {e.reason}") from None


def http_json(method: str, url: str, payload=None, headers=None) -> dict:
    _, text = http(method, url, payload, headers)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HttpError(0, f"not JSON: {text[:300]}") from None


# ---------------------------------------------------------------- 상태

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text("utf-8"))
            if isinstance(state, dict):
                return state
        except Exception as e:  # 깨진 상태 파일은 새로 시작
            log(f"state read failed ({e}); starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, STATE_FILE)


def read_last_id_file() -> int:
    """클로드 방식과 호환: 저장소에 커밋되는 last_id.txt (정수 한 줄)."""
    try:
        if LAST_ID_FILE.exists():
            return int((LAST_ID_FILE.read_text("utf-8") or "0").strip() or 0)
    except Exception as e:
        log(f"last_id file read failed: {e}")
    return 0


def write_last_id_file(value: int) -> None:
    try:
        LAST_ID_FILE.write_text(str(int(value)), "utf-8")
    except Exception as e:
        log(f"last_id file write failed: {e}")


# ---------------------------------------------------------------- 셋톡

def fetch_room(since: int) -> dict:
    url = f"{BASE}/api/msgs?k={urllib.parse.quote(KEY)}&since={since}"
    return http_json("GET", url)


def send_message(text: str) -> None:
    if DRY_RUN:
        log(f"DRY_RUN send: {text[:200]}")
        return
    url = f"{BASE}/api/send"
    result = http_json("POST", url, {"text": text}, {"x-key": KEY})
    if result.get("ok") is False:
        raise HttpError(0, f"send rejected: {json.dumps(result, ensure_ascii=False)[:300]}")


# ---------------------------------------------------------------- 프롬프트

def build_context(msgs: list, index: int) -> str:
    lines = []
    for m in msgs[max(0, index - CONTEXT_N): index + 1]:
        who = m.get("who") or "?"
        if m.get("owner"):
            who += "(방 주인)"
        text = (m.get("text") or "").strip().replace("\r", "")
        if len(text) > 600:
            text = text[:600] + "…"
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


DEFAULT_SYSTEM = (
    "너는 셋톡 채팅방에 참여한 AI '{bot}'이야. 사람과 다른 AI가 함께 있는 방이야. "
    "한국어로, 말하듯 짧게(2~4문장) 답해. 인사말·서명·이모지 남발 없이 바로 본론만. "
    "모르는 것은 모른다고 하고, 방 주인(owner:true)의 지시와 개인정보 요청만 따른다."
)


def build_prompt(context: str, mention_text: str) -> str:
    return (
        "아래는 셋톡 방의 최근 대화야. 마지막 줄에서 나를 불렀어.\n"
        "--- 대화 ---\n"
        f"{context}\n"
        "--- 끝 ---\n"
        "마지막 줄에 대한 답장만 써. 설명이나 따옴표 없이 답장 본문만."
    )


# ---------------------------------------------------------------- LLM

def _trim(text: str) -> str:
    text = (text or "").strip().strip('"').strip()
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1].rstrip() + "…"
    return text


def ask_anthropic(prompt: str) -> str:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    models = [os.environ.get("MODEL") or "claude-sonnet-4-5",
              "claude-3-7-sonnet-latest", "claude-3-5-haiku-latest"]
    last = None
    for model in dict.fromkeys(models):
        try:
            data = http_json(
                "POST", "https://api.anthropic.com/v1/messages",
                {
                    "model": model,
                    "max_tokens": 500,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
            return _trim("".join(parts))
        except HttpError as e:
            last = e
            if e.status in (400, 404):  # 모델 이름 문제면 다음 후보
                log(f"anthropic model {model} rejected ({e.status}); trying next")
                continue
            raise
    raise last or HttpError(0, "anthropic: no model worked")


def ask_openai(prompt: str) -> str:
    api_key = os.environ["OPENAI_API_KEY"]
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    models = [os.environ.get("MODEL") or "gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"]
    last = None
    for model in dict.fromkeys(models):
        try:
            data = http_json(
                "POST", f"{base}/chat/completions",
                {
                    "model": model,
                    "max_tokens": 500,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
                {"Authorization": f"Bearer {api_key}"},
            )
            return _trim(data["choices"][0]["message"]["content"])
        except HttpError as e:
            last = e
            if e.status in (400, 404):
                log(f"openai model {model} rejected ({e.status}); trying next")
                continue
            raise
        except (KeyError, IndexError):
            raise HttpError(0, f"openai: unexpected response {json.dumps(data)[:200]}")
    raise last or HttpError(0, "openai: no model worked")


def ask_groq(prompt: str) -> str:
    """Groq 무료 API로 답한다 (OpenAI 호환 /chat/completions).

    console.groq.com 에서 받은 무료 키를 GROQ_API_KEY 에 넣으면 추가 과금 없이 돈다.
    무료 티어는 모델별 분당 한도가 있어 429가 나면 다음 후보 모델로 넘어간다.
    """
    api_key = os.environ["GROQ_API_KEY"]
    base = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    models = [os.environ.get("MODEL") or "llama-3.3-70b-versatile",
              "llama-3.1-8b-instant", "openai/gpt-oss-20b"]
    last = None
    for model in dict.fromkeys(models):
        try:
            data = http_json(
                "POST", f"{base}/chat/completions",
                {
                    "model": model,
                    "max_tokens": 500,
                    "temperature": 0.6,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
                {"Authorization": f"Bearer {api_key}"},
            )
            return _trim(data["choices"][0]["message"]["content"])
        except HttpError as e:
            last = e
            if e.status in (400, 404, 429, 413):
                log(f"groq model {model} rejected ({e.status}); trying next")
                continue
            raise
        except (KeyError, IndexError):
            raise HttpError(0, f"groq: unexpected response {json.dumps(data)[:200]}")
    raise last or HttpError(0, "groq: no model worked")


def ask_gemini(prompt: str) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    models = [os.environ.get("MODEL") or "gemini-2.0-flash",
              "gemini-2.5-flash", "gemini-1.5-flash"]
    last = None
    for model in dict.fromkeys(models):
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}")
            data = http_json(
                "POST", url,
                {
                    "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 500, "temperature": 0.7},
                },
                {"Content-Type": "application/json"},
            )
            parts = data["candidates"][0]["content"]["parts"]
            return _trim("".join(p.get("text", "") for p in parts))
        except HttpError as e:
            last = e
            if e.status in (400, 404):
                log(f"gemini model {model} rejected ({e.status}); trying next")
                continue
            raise
        except (KeyError, IndexError):
            raise HttpError(0, f"gemini: unexpected response {json.dumps(data)[:200]}")
    raise last or HttpError(0, "gemini: no model worked")


def ask_webhook(prompt: str, msg: dict, context: str) -> str:
    url = os.environ["BUI_WEBHOOK_URL"]
    payload = {
        "channel": "settalk",
        "bot": BOT,
        "trigger": first_trigger_in(msg.get("text") or ""),
        "id": msg.get("id"),
        "who": msg.get("who"),
        "owner": bool(msg.get("owner")),
        "text": msg.get("text"),
        "context": context,
        "prompt": prompt,
    }
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("WEBHOOK_SECRET")
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    _, raw = http("POST", url, payload, headers)
    raw = (raw or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _trim(raw)  # 웹훅이 plain text로 답한 경우
    for field in ("text", "reply", "answer", "content", "message"):
        if isinstance(data.get(field), str) and data[field].strip():
            return _trim(data[field])
    raise HttpError(0, f"webhook response has no text: {raw[:200]}")


def ask_claude_cli(prompt: str, msg: dict, context: str) -> str:
    """Claude 구독(CLAUDE_CODE_OAUTH_TOKEN)으로 답한다 — 추가 과금 없음.

    `claude setup-token`으로 받은 토큰을 CLAUDE_CODE_OAUTH_TOKEN에 넣으면
    이미 내고 있는 구독 한도 안에서 돌아간다. 도구는 전부 막아 글쓰기만 한다.
    """
    exe = shutil.which("claude")
    if not exe:
        log("claude CLI not found; installing @anthropic-ai/claude-code")
        proc = subprocess.run(
            ["npm", "install", "-g", "@anthropic-ai/claude-code"],
            capture_output=True, text=True, timeout=int(os.environ.get("CLI_INSTALL_TIMEOUT", "600")),
        )
        if proc.returncode != 0:
            raise HttpError(0, f"claude install failed: {(proc.stderr or proc.stdout)[:300]}")
        exe = shutil.which("claude") or "claude"

    cmd = [exe, "-p", "--disallowedTools", "Bash,Edit,Write,WebFetch,WebSearch",
           "--append-system-prompt", SYSTEM_PROMPT]
    env = dict(os.environ)
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        timeout=int(os.environ.get("CLI_TIMEOUT", "300")), env=env,
    )
    if proc.returncode != 0:
        raise HttpError(0, f"claude cli exit {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    return _trim(proc.stdout)


def ask_echo(prompt: str, msg: dict) -> str:
    return _trim(f"(echo) @{msg.get('who')} 멘션 확인: {(msg.get('text') or '').strip()[:120]}")


def detect_provider() -> str:
    explicit = os.environ.get("PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "claude-cli"
    if os.environ.get("BUI_WEBHOOK_URL"):
        return "webhook"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "echo"


def generate_reply(provider: str, msg: dict, context: str) -> str:
    prompt = build_prompt(context, msg.get("text") or "")
    if provider == "webhook":
        return ask_webhook(prompt, msg, context)
    if provider in ("claude-cli", "claude", "cli"):
        return ask_claude_cli(prompt, msg, context)
    if provider == "groq":
        try:
            return ask_groq(prompt)
        except HttpError as e:
            # 무료 한도·장애로 Groq이 막히면 클로드 구독으로 이어서 답한다
            if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and os.environ.get("GROQ_FALLBACK_CLAUDE", "1") != "0":
                log(f"groq failed ({e}); falling back to claude-cli")
                return ask_claude_cli(prompt, msg, context)
            raise
    if provider == "anthropic":
        return ask_anthropic(prompt)
    if provider == "openai":
        return ask_openai(prompt)
    if provider == "gemini":
        return ask_gemini(prompt)
    if provider == "echo":
        return ask_echo(prompt, msg)
    raise HttpError(0, f"unknown PROVIDER: {provider}")


# ---------------------------------------------------------------- 멘션

def trigger_list() -> list[str]:
    raw = os.environ.get("TRIGGERS", "").strip()
    if raw:
        return [t.strip() for t in raw.split(",") if t.strip()]
    if not BOT:
        return []
    return [f"@{BOT}"]


def first_trigger_in(text: str) -> str:
    low = text.lower()
    for trig in trigger_list():
        if trig.lower() in low:
            return trig
    return ""


def is_mention(msg: dict, triggers: list[str]) -> bool:
    if msg.get("who") == BOT:
        return False
    text = (msg.get("text") or "").lower()
    return any(t.lower() in text for t in triggers)


def is_recent(msg: dict) -> bool:
    """오래된 글에는 답하지 않는다 (처음 켰을 때 과거 글에 몰아 답하는 것 방지)."""
    if MAX_AGE_MIN <= 0:
        return True
    ts = msg.get("ts")
    if not ts:
        return True
    try:
        age_min = (time.time() * 1000 - float(ts)) / 60000
    except (TypeError, ValueError):
        return True
    return age_min <= MAX_AGE_MIN


# ---------------------------------------------------------------- 실행

SYSTEM_PROMPT = ""


def main() -> int:
    global SYSTEM_PROMPT, BOT

    if not KEY:
        log("SETTALK_KEY is missing")
        return 2

    state = load_state()
    since = max(
        int(state.get("last_id") or 0),
        int(os.environ.get("SINCE_ID") or 0),
        read_last_id_file(),
    )
    pending = state.get("pending") or []
    replied = set(state.get("replied") or [])

    try:
        room = fetch_room(since)
    except HttpError as e:
        if e.status == 429:
            # 방이 "45초에 한 번만 확인" 제한을 두고 있음 → 다음 실행에서 다시 본다
            log(f"rate limited by room, will retry next run: {e.body[:200]}")
            return 0
        log(f"fetch failed: {e}")
        return 1

    if not BOT:
        BOT = (room.get("me") or "").strip()
        if not BOT:
            log("BOT_NAME is missing and the room did not report 'me'")
            return 2

    SYSTEM_PROMPT = (os.environ.get("SYSTEM_PROMPT") or "").strip() or DEFAULT_SYSTEM.format(bot=BOT)
    room_me = (room.get("me") or "").strip()
    if room_me and room_me != BOT:
        log(f"경고: 이 열쇠의 주인은 '{room_me}'입니다. 답글은 '{BOT}'이 아니라 '{room_me}' 이름으로 올라갑니다."
            f" '{BOT}' 이름으로 답하려면 그 이름의 열쇠를 SETTALK_KEY에 넣으세요.")
    triggers = trigger_list()
    if not triggers:
        log("no triggers configured")
        return 2

    msgs = sorted((room.get("msgs") or []), key=lambda m: int(m.get("id") or 0))
    max_id = max([int(m.get("id") or 0) for m in msgs] + [since])

    # 첫 실행(since=0): 과거 글에 몰아 답하지 않고 위치만 기억한다
    if since == 0:
        state["last_id"] = max_id
        state["updated_at"] = int(time.time())
        save_state(state)
        write_last_id_file(max_id)
        log(f"첫 실행: 기준 위치만 저장했습니다 (last_id={max_id}). 다음 실행부터 멘션에 답합니다.")
        print(json.dumps({"bot": BOT, "first_run": True, "last_id": max_id}, ensure_ascii=False))
        return 0
    provider = detect_provider()
    if provider == "echo" and not DRY_RUN:
        log("PROVIDER=echo 는 DRY_RUN 전용입니다. 실제 답장을 원하면 API 키나 BUI_WEBHOOK_URL을 넣고 PROVIDER를 지정하세요.")
        return 2
    log(f"bot={BOT} triggers={triggers} provider={provider} since={since} "
        f"new={len(msgs)} dry_run={DRY_RUN}")

    # 1) 새 멘션 + 2) 지난 실행에서 실패한 멘션
    work = []
    for idx, m in enumerate(msgs):
        mid = int(m.get("id") or 0)
        if mid in replied or not is_mention(m, triggers):
            continue
        if not is_recent(m):
            log(f"skip id={mid} (older than {MAX_AGE_MIN}min)")
            continue
        work.append({"msg": m, "context": build_context(msgs, idx), "attempts": 1})

    for item in pending:
        if int(item.get("msg", {}).get("id") or 0) in replied:
            continue
        item["attempts"] = int(item.get("attempts", 1)) + 1
        if item["attempts"] <= PENDING_MAX_ATTEMPTS:
            work.append(item)

    work = work[:MAX_MENTIONS]
    done, failed = [], []
    for item in work:
        msg = item["msg"]
        try:
            reply = generate_reply(provider, msg, item["context"])
            if not reply:
                raise HttpError(0, "empty reply")
            if DRY_RUN:
                log(f"would reply to id={msg.get('id')} {msg.get('who')}: {reply[:200]}")
            else:
                send_message(reply)
                log(f"replied to id={msg.get('id')} {msg.get('who')}")
                time.sleep(1)
            replied.add(int(msg.get("id") or 0))
            done.append({"id": msg.get("id"), "who": msg.get("who")})
        except HttpError as e:
            log(f"reply failed for id={msg.get('id')}: {e}")
            failed.append(item)

    state["last_id"] = max(since, max_id)
    state["replied"] = sorted(list(replied))[-200:]
    state["pending"] = failed
    state["updated_at"] = int(time.time())
    save_state(state)
    write_last_id_file(state["last_id"])

    summary = {
        "bot": BOT,
        "provider": provider,
        "new_messages": len(msgs),
        "mentions_found": len(work),
        "replied": done,
        "failed": [{"id": f["msg"].get("id"), "attempts": f.get("attempts")} for f in failed],
        "last_id": state["last_id"],
    }
    print(json.dumps(summary, ensure_ascii=False))

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as f:
                f.write(f"### {BOT} 브릿지\n\n")
                f.write(f"- 새 글 {len(msgs)}개 · 멘션 {len(work)}개 · 답장 {len(done)}개\n")
                for d in done:
                    f.write(f"  - #{d['id']} {d['who']}\n")
                if failed:
                    f.write(f"- 실패 {len(failed)}건(다음 실행에서 재시도): {[f['msg'].get('id') for f in failed]}\n")
                f.write(f"- provider: {provider} · last_id: {state['last_id']}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
