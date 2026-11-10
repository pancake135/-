"""Upstage API 키·워크플로우 ID 점검 테스트

실행:  python test_api.py                      (1~4단계: 키·업로드·ID까지)
       python test_api.py 상품요약서.pdf         (5단계: 실제 진단 한 번까지)

키와 ID는 .streamlit/secrets.toml → 환경변수 → 직접 입력 순서로 찾는다.
키 값은 화면에 앞 5자·뒤 3자만 보인다.
"""
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

V1 = os.environ.get("UPSTAGE_V1_URL", "https://api.upstage.ai/v1")
V2 = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v2")
HERE = Path(__file__).parent
SECRET_FILES = [HERE / ".streamlit" / "secrets.toml", Path.cwd() / ".streamlit" / "secrets.toml",
                Path.home() / ".streamlit" / "secrets.toml", HERE / "secrets.toml", HERE / "secrets.toml.txt"]

results = []


def clean(v):
    v = (v or "").replace("﻿", "").replace("​", "").replace(" ", " ").strip().strip('"').strip("'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return re.sub(r"\s+", "", v)


def find(name):
    for p in SECRET_FILES:
        try:
            m = re.search(rf"^\s*{name}\s*[=:]\s*(.+)$", p.read_text(encoding="utf-8-sig"), re.M)
        except Exception:
            continue
        if m and clean(m.group(1)):
            return clean(m.group(1)), str(p)
    if os.environ.get(name):
        return clean(os.environ[name]), "환경변수"
    return "", ""


def mask(k):
    return f"{k[:5]}…{k[-3:]}" if len(k) > 10 else "(너무 짧음)"


def report(step, ok, msg):
    mark = "통과" if ok else ("건너뜀" if ok is None else "실패")
    results.append((step, mark, msg))
    print(f"[{mark}] {step}: {msg}")


def body_of(r):
    try:
        return json.dumps(r.json(), ensure_ascii=False)[:300]
    except Exception:
        return r.text[:300]


def why(code):
    return {401: "키가 틀렸거나 만료됨", 403: "이 키에 권한이 없음(Studio 미사용 계정·조직 키일 수 있음)",
            404: "주소나 ID를 찾지 못함", 409: "파일 변환 중", 415: "파일 형식 미지원",
            429: "호출 한도 초과"}.get(code, "")


def tiny_pdf():
    # 글자 한 줄짜리 1쪽 PDF (업로드 시험용)
    objs = ["<</Type/Catalog/Pages 2 0 R>>", "<</Type/Pages/Kids[3 0 R]/Count 1>>",
            "<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            None, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    stream = b"BT /F1 12 Tf 20 50 Td (API key test) Tj ET"
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        if o is None:
            out += f"{i} 0 obj<</Length {len(stream)}>>stream\n".encode() + stream + b"\nendstream endobj\n"
        else:
            out += f"{i} 0 obj{o}endobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer<</Size {len(objs)+1}/Root 1 0 R>>\nstartxref\n{x}\n%%EOF\n".encode()
    return out


def main():
    print("=== Upstage API 키 점검 ===\n")
    key, ksrc = find("UPSTAGE_API_KEY")
    if not key:
        key = clean(getpass.getpass("API 키를 붙여넣고 Enter (화면에 안 보임): "))
        ksrc = "직접 입력"
    agt, asrc = find("STUDIO_AGENT_ID")
    if not agt:
        agt = clean(input("워크플로우 ID(agt_…)를 붙여넣고 Enter (없으면 그냥 Enter): "))
        asrc = "직접 입력"

    # 1. 형식
    ok = key.startswith("up_") and len(key) > 20
    report("1. 키 형식", ok, f"{mask(key)} ({len(key)}자, {ksrc}에서 읽음)"
           + ("" if ok else " → up_ 로 시작하는 Upstage 콘솔 API 키인지 확인"))
    if not key:
        return
    H = {"Authorization": f"Bearer {key}"}

    # 2. 키 자체가 살아 있는지 (가장 가벼운 채팅 호출, 1토큰)
    try:
        r = requests.post(f"{V1}/chat/completions", headers=H, timeout=30,
                          json={"model": "solar-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
        report("2. 키 인증 (v1 Solar 호출)", r.ok,
               "키가 정상 동작" if r.ok else f"HTTP {r.status_code} {why(r.status_code)} {body_of(r)}")
    except requests.RequestException as e:
        report("2. 키 인증 (v1 Solar 호출)", False, f"네트워크 오류: {e} → 인터넷·회사 방화벽·프록시 확인")
        return

    # 3. Studio(v2) 파일 업로드·삭제
    fid = None
    try:
        r = requests.post(f"{V2}/files", headers=H, timeout=60,
                          files={"file": ("api_test.pdf", tiny_pdf(), "application/pdf")}, data={"purpose": "user_data"})
        if r.ok:
            fid = r.json().get("id")
            report("3. Studio 파일 업로드 (v2)", True, f"업로드됨 file_id={fid}")
        else:
            report("3. Studio 파일 업로드 (v2)", False, f"HTTP {r.status_code} {why(r.status_code)} {body_of(r)}")
    except requests.RequestException as e:
        report("3. Studio 파일 업로드 (v2)", False, f"네트워크 오류: {e}")
    finally:
        if fid:
            try:
                requests.delete(f"{V2}/files/{fid}", headers=H, timeout=30)
            except requests.RequestException:
                pass

    # 4. 워크플로우 ID
    if not agt:
        report("4. 워크플로우 ID", None, "ID를 넣지 않아 건너뜀")
    else:
        fmt = agt.startswith("agt_")
        try:
            r = requests.get(f"{V2}/agents/{agt}", headers=H, timeout=30)
            if r.ok:
                name = r.json().get("name", "")
                report("4. 워크플로우 ID", True, f"{agt} 찾음 {name}")
            elif r.status_code == 404:
                report("4. 워크플로우 ID", False if not fmt else None,
                       f"{agt} 조회 안 됨(404)" + ("" if fmt else " → agt_ 로 시작하는 ID가 아님")
                       + " → 5단계(실제 진단)로 최종 확인하세요")
            else:
                report("4. 워크플로우 ID", False, f"HTTP {r.status_code} {why(r.status_code)} {body_of(r)}")
        except requests.RequestException as e:
            report("4. 워크플로우 ID", False, f"네트워크 오류: {e}")

    # 5. 실제 진단 한 번 (파일을 줬을 때만)
    if len(sys.argv) > 1 and agt:
        path = Path(sys.argv[1])
        try:
            import base64
            raw = path.read_bytes()
            mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/jpeg"
            fid = None
            body = {"model": agt, "include": "all", "input": [{"role": "user", "content": [
                {"type": "input_file", "filename": path.name, "file_data": f"data:{mime};base64," + base64.b64encode(raw).decode()}]}]}
            r = requests.post(f"{V2}/responses", headers=H, json=body, timeout=180)
            print(f"    방법 A(파일 바로 첨부): HTTP {r.status_code} {'' if r.ok else body_of(r)}")
            if not r.ok:
                u = requests.post(f"{V2}/files", headers=H, timeout=120, files={"file": (path.name, raw, mime)}, data={"purpose": "user_data"})
                u.raise_for_status()
                fid = u.json()["id"]
                for _ in range(40):
                    st = requests.get(f"{V2}/files/{fid}", headers=H, params={"view": "status"}, timeout=20)
                    status = (st.json().get("status") or "").upper() if st.ok else ""
                    print(f"    업로드 파일 상태: {status or st.status_code}")
                    if status in ("UPLOADED", "READY", "FAILED") or not st.ok:
                        break
                    time.sleep(3)
                body["input"][0]["content"] = [{"type": "input_file", "file_id": fid}]
                for _ in range(12):
                    r = requests.post(f"{V2}/responses", headers=H, json=body, timeout=180)
                    if r.status_code != 409:
                        break
                    time.sleep(5)
                print(f"    방법 B(업로드 후 file_id): HTTP {r.status_code} {'' if r.ok else body_of(r)}")
            r.raise_for_status()
            job = r.json()
            print(f"    작업 시작 id={job.get('id')} 상태={job.get('status')} (5초마다 확인)")
            t0 = time.time()
            while job.get("status") in ("queued", "in_progress") and time.time() - t0 < 600:
                time.sleep(5)
                job = requests.get(f"{V2}/responses/{job['id']}", headers=H, params={"include": "all"}, timeout=60).json()
                print(f"    … {int(time.time()-t0)}초 상태={job.get('status')}")
            if fid:
                requests.delete(f"{V2}/files/{fid}", headers=H, timeout=30)
            ok = job.get("status") == "completed"
            out = HERE / "test_result.json"
            out.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
            steps = [s.get("model") or s.get("type") for s in job.get("output") or []]
            report("5. 실제 진단", ok, f"상태={job.get('status')} 단계={steps} → 전체 응답 {out.name}에 저장"
                   + ("" if ok else f" 실패 사유: {job.get('failure_message') or job.get('error')}"))
        except requests.HTTPError as e:
            report("5. 실제 진단", False, f"HTTP {e.response.status_code} {why(e.response.status_code)} {body_of(e.response)}")
        except Exception as e:
            report("5. 실제 진단", False, f"{type(e).__name__}: {e}")
    elif agt:
        report("5. 실제 진단", None, "파일을 주지 않아 건너뜀 (python test_api.py 파일.pdf)")

    print("\n=== 요약 ===")
    for s, m, _ in results:
        print(f"  {m:4}  {s}")
    print("\n이 화면을 복사해 보내 주세요. 키는 앞 5자·뒤 3자만 보이니 그대로 보내도 됩니다.")


if __name__ == "__main__":
    main()
    if os.name == "nt" and sys.stdin.isatty():
        input("\nEnter를 누르면 창이 닫힙니다.")
