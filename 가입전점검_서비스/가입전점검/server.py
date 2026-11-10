"""가입 전 점검 — 암보험 상품요약서 진단 서비스 (서버)

실행:  python server.py        → 브라우저에서 http://localhost:8000
설정:  .streamlit/secrets.toml 또는 secrets.toml 에 UPSTAGE_API_KEY, STUDIO_AGENT_ID
       (없으면 화면 오른쪽 위 '연결 설정'에서 입력)
"""
import base64
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import requests
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

BASE_URL = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v2")
HERE = Path(__file__).parent
WEB = HERE / "web"
SAMPLE = HERE / "sample_result.json"
VERDICTS = ["초록", "노랑", "빨강", "검토 필요"]
GUIDE_NODES = {"초록_가입안내": "초록", "노랑_주의안내": "노랑", "빨강_정정요청": "빨강",
               "검토필요_보완요청": "검토 필요", "미분류_안내": "미분류"}
SECRET_FILES = [HERE / ".streamlit" / "secrets.toml", Path.cwd() / ".streamlit" / "secrets.toml",
                Path.home() / ".streamlit" / "secrets.toml", HERE / "secrets.toml", HERE / "secrets.toml.txt"]


# ---------- 설정 ----------
def clean_value(v: str) -> str:
    v = (v or "").replace("﻿", "").replace("​", "").replace(" ", " ").strip().strip('"').strip("'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    if "=" in v and not v.startswith(("up_", "agt_")):
        v = v.split("=", 1)[1].strip().strip('"').strip("'").strip()
    return re.sub(r"\s+", "", v)


def read_setting(name: str):
    for p in SECRET_FILES:
        try:
            m = re.search(rf"^\s*{name}\s*[=:]\s*(.+)$", p.read_text(encoding="utf-8-sig"), re.M)
        except Exception:
            continue
        if m and clean_value(m.group(1)):
            return clean_value(m.group(1)), p.name
    if os.environ.get(name):
        return clean_value(os.environ[name]), "환경변수"
    return "", ""


CONFIG = {}
CONFIG["key"], CONFIG["key_src"] = read_setting("UPSTAGE_API_KEY")
CONFIG["agent"], CONFIG["agent_src"] = read_setting("STUDIO_AGENT_ID")
JOBS = {}  # 우리 job id → {upstream id, file id, filename, started}


def headers():
    if not CONFIG["key"]:
        raise HTTPException(400, "연결 설정에서 API 키를 입력해 주세요.")
    return {"Authorization": f"Bearer {CONFIG['key']}"}


def explain(r: requests.Response) -> str:
    try:
        body = r.json()
        msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
    except Exception:
        msg = r.text[:200]
    hint = {401: "API 키가 맞지 않습니다. 콘솔에서 키를 다시 복사해 주세요.",
            403: "이 키로는 해당 워크플로우나 파일에 접근할 수 없습니다. API 키를 만든 조직(워크스페이스)과 Studio 워크플로우가 있는 조직이 같은지 확인해 주세요.",
            404: "워크플로우 ID를 찾지 못했습니다. 게시한 워크플로우의 ID(agt_…)인지 확인해 주세요.",
            415: "PDF, PNG, JPG 파일만 올릴 수 있습니다.",
            429: "요청이 많아 잠시 후 다시 시도해 주세요."}.get(r.status_code, "Upstage 서버에서 오류가 났습니다.")
    return f"{hint} (HTTP {r.status_code}{', ' + msg if msg else ''})"


# ---------- 결과 해석 ----------
def step_texts(result: dict):
    out = []
    for step in result.get("output") or []:
        name = step.get("model") or step.get("name") or step.get("type") or ""
        parts = []
        for c in step.get("content") or []:
            if isinstance(c, dict):
                t = c.get("text")
                if t is None and c.get("json") is not None:
                    t = json.dumps(c["json"], ensure_ascii=False)
                if t:
                    parts.append(t)
            elif isinstance(c, str):
                parts.append(c)
        out.append({"name": name, "text": "\n".join(parts)})
    return out


def tidy_steps(steps):
    out = []
    for s in steps:
        t = s["text"]
        j = find_json(t)
        t = json.dumps(j, ensure_ascii=False, indent=1) if j else strip_marks(t)
        out.append({"name": s["name"], "text": t})
    return out


def find_json(text: str):
    text = re.sub(r"```(?:json)?", "", text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and ("file_verdict" in obj or "plans" in obj):
            return obj
    return None


def unescape(text: str) -> str:
    """글자로 남은 \\n, \\t, \\" 같은 이스케이프 표기를 실제 줄바꿈·문자로 바꾼다."""
    if not isinstance(text, str):
        return text
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n").replace("\\t", " ")
    return text.replace('\\"', '"').replace("\\/", "/")


def strip_marks(text: str) -> str:
    """안내문 정리: JSON으로 감싸진 글이면 풀고, 이스케이프 표기·출처 기호·남는 빈 줄을 지운다."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    for _ in range(2):
        if t[:1] in ('"', "{", "["):
            try:
                obj = json.loads(t)
            except ValueError:
                break
            if isinstance(obj, str):
                t = obj.strip()
                continue
            if isinstance(obj, dict) and not ("file_verdict" in obj or "plans" in obj):
                strs = [v for v in obj.values() if isinstance(v, str)]
                if strs:
                    t = max(strs, key=len).strip()
                    continue
            if isinstance(obj, list) and obj and all(isinstance(x, str) for x in obj):
                t = "\n".join(obj)
                continue
        break
    t = unescape(t)
    if len(t) > 1 and t[0] == t[-1] == '"':
        t = t[1:-1]
    t = re.sub(r"【[^】]*】", "", t)
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def interpret(result: dict, filename: str) -> dict:
    steps = step_texts(result)
    vj, guide_node, guide = None, None, ""
    for s in steps:
        j = find_json(s["text"])
        if j and vj is None:
            vj = j
        for node in GUIDE_NODES:
            if node in s["name"]:
                guide_node, guide = node, s["text"]
    verdict = None
    if vj and vj.get("file_verdict") in VERDICTS:
        verdict = vj["file_verdict"]
    elif guide_node:
        verdict = GUIDE_NODES[guide_node]
    if not guide:
        for s in reversed(steps):
            if s["text"].strip() and not find_json(s["text"]):
                guide = s["text"]
                break
    if verdict is None:
        verdict = next((v for v in VERDICTS if any(v in s["text"] for s in steps[-2:])), "검토 필요")
    plans = []
    for p in (vj or {}).get("plans") or []:
        row = {k: p.get(k) for k in ("plan", "monthly_premium", "verdict", "calc_errors", "risk_signals",
                                       "reason", "e1_check", "e2_check", "e3_check")}
        for k, v in row.items():
            if isinstance(v, str):
                row[k] = unescape(v).strip()
            elif isinstance(v, list):
                row[k] = [unescape(x).strip() if isinstance(x, str) else x for x in v]
        plans.append(row)
    return {"filename": filename, "verdict": verdict,
            "product_name": (vj or {}).get("product_name", ""), "edition_code": (vj or {}).get("edition_code", ""),
            "ignored_documents": (vj or {}).get("ignored_documents") or [],
            "plans": plans, "guide": strip_marks(guide), "guide_node": guide_node, "steps": tidy_steps(steps)}


# ---------- API ----------
app = FastAPI(title="가입 전 점검")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/assets/{name}")
def asset(name: str):
    p = (WEB / name).resolve()
    if WEB.resolve() not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/config")
def get_config():
    k = CONFIG["key"]
    return {"key_set": bool(k), "key_masked": f"{k[:5]}…{k[-3:]}" if len(k) > 10 else "",
            "key_src": CONFIG["key_src"], "agent": CONFIG["agent"], "agent_src": CONFIG["agent_src"]}


@app.post("/api/config")
async def set_config(req: Request):
    body = await req.json()
    if body.get("key"):
        CONFIG["key"], CONFIG["key_src"] = clean_value(body["key"]), "화면 입력"
    if body.get("agent") is not None:
        CONFIG["agent"], CONFIG["agent_src"] = clean_value(body["agent"]), "화면 입력"
    return get_config()


@app.post("/api/check")
def check():
    h = headers()
    try:
        r = requests.get(f"{BASE_URL}/agents/{CONFIG['agent']}", headers=h, timeout=20)
        if r.ok:
            return {"ok": True, "message": "연결되었습니다. 키와 워크플로우 ID가 확인되었습니다."}
        if r.status_code == 404:
            r2 = requests.get(f"{BASE_URL}/files", headers=h, params={"limit": 1}, timeout=20)
            if r2.ok:
                return {"ok": True, "message": "API 키는 확인되었습니다. 워크플로우 ID는 첫 진단 때 확인됩니다."}
            return {"ok": False, "message": explain(r2)}
        return {"ok": False, "message": explain(r)}
    except requests.RequestException as e:
        return {"ok": False, "message": f"Upstage 서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요. ({e.__class__.__name__})"}


def start_job(h, content_item):
    body = {"model": CONFIG["agent"], "include": "all",
            "input": [{"role": "user", "content": [content_item]}]}
    for _ in range(24):  # 파일 변환 중(409)이면 잠시 후 다시
        r = requests.post(f"{BASE_URL}/responses", headers=h, json=body, timeout=180)
        if r.status_code in (400, 422) and "include" in r.text and isinstance(body["include"], str):
            body["include"] = [body["include"]]  # 목록 형식만 받는 경우
            continue
        if r.status_code != 409:
            return r
        time.sleep(5)
    return r


def wait_uploaded(h, fid, limit=120):
    """업로드한 파일이 페이지 이미지 변환을 마칠 때까지(UPLOADED/READY) 기다린다."""
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            r = requests.get(f"{BASE_URL}/files/{fid}", headers=h, params={"view": "status"}, timeout=20)
            st = (r.json().get("status") or "").upper() if r.ok else ""
        except Exception:
            st = ""
        if st in ("UPLOADED", "READY"):
            return True
        if st == "FAILED":
            return False
        if not st and time.time() - t0 > 10:
            return True  # 상태를 안 알려 주면 그냥 진행
        time.sleep(3)
    return True


@app.post("/api/diagnose")
def diagnose(file: UploadFile = File(...)):
    h = headers()
    if not CONFIG["agent"]:
        raise HTTPException(400, "연결 설정에서 워크플로우 ID를 입력해 주세요.")
    data = file.file.read()
    mime = file.content_type or ("application/pdf" if file.filename.lower().endswith(".pdf") else "image/jpeg")
    fid = None
    try:
        # 1순위: 파일을 요청에 바로 실어 보낸다 (별도 업로드 파일의 접근 권한 문제를 피함)
        item = {"type": "input_file", "filename": file.filename,
                "file_data": f"data:{mime};base64," + base64.b64encode(data).decode()}
        r = start_job(h, item)
        if not r.ok and r.status_code not in (401, 404, 429):
            # 2순위: 업로드 → 변환 완료 대기 → file_id 로 실행
            u = requests.post(f"{BASE_URL}/files", headers=h, timeout=120,
                              files={"file": (file.filename, data, mime)}, data={"purpose": "user_data"})
            if not u.ok:
                raise HTTPException(502, explain(u))
            fid = u.json()["id"]
            if not wait_uploaded(h, fid):
                raise HTTPException(502, "Upstage에서 파일 변환에 실패했습니다. 다른 형식(PDF/JPG)으로 다시 올려 주세요.")
            r = start_job(h, {"type": "input_file", "file_id": fid})
        if not r.ok:
            if fid:
                requests.delete(f"{BASE_URL}/files/{fid}", headers=h, timeout=20)
            raise HTTPException(502, explain(r))
    except requests.RequestException as e:
        raise HTTPException(502, f"Upstage 서버에 연결하지 못했습니다. ({e.__class__.__name__})")
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"up": r.json()["id"], "fid": fid, "filename": file.filename, "started": time.time()}
    return {"job": jid}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "진단 작업을 찾지 못했습니다.")
    if "result" in j:
        return {"status": "completed", "elapsed": j["elapsed"], "result": j["result"]}
    h = headers()
    try:
        r = requests.get(f"{BASE_URL}/responses/{j['up']}", headers=h, params={"include": "all"}, timeout=60)
        if r.status_code in (400, 422):
            r = requests.get(f"{BASE_URL}/responses/{j['up']}", headers=h, timeout=60)
    except requests.RequestException:
        return {"status": "in_progress", "elapsed": int(time.time() - j["started"])}
    if not r.ok:
        raise HTTPException(502, explain(r))
    job = r.json()
    st = job.get("status")
    elapsed = int(time.time() - j["started"])
    if st in ("queued", "in_progress"):
        return {"status": st, "elapsed": elapsed}
    if j.get("fid"):
        try:
            requests.delete(f"{BASE_URL}/files/{j['fid']}", headers=h, timeout=20)
        except requests.RequestException:
            pass
    if st != "completed":
        raise HTTPException(502, f"진단이 끝나지 못했습니다: {job.get('failure_message') or job.get('error') or st}")
    j["result"], j["elapsed"] = interpret(job, j["filename"]), elapsed
    return {"status": "completed", "elapsed": elapsed, "result": j["result"]}


@app.get("/api/sample")
def sample():
    s = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return {"status": "completed", "elapsed": 0, "result": interpret(s["result"], s["filename"]), "sample": True}


@app.exception_handler(HTTPException)
async def http_err(_, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    print(f"\n  가입 전 점검 서비스: http://localhost:{port}\n  (끄려면 이 창에서 Ctrl+C)\n")
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=port, log_level="warning")
