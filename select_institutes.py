from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
GEMINI_MODEL = "gemini-2.5-flash"
N = 10

PHASE1_JSON = ROOT / "output" / "student_output.json"
OUTPUT_JSON = ROOT / "output" / "phase2_institutes.json"


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _response_text(response) -> str:
    text = (getattr(response, "text", None) or "").strip()
    if text:
        return text
    chunks: list[str] = []
    for cand in getattr(response, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            t = getattr(part, "text", None)
            if t:
                chunks.append(t)
    return "\n".join(chunks).strip()


def _parse_json_payload(text: str) -> dict:
    cleaned = _strip_json_fences(text)
    if cleaned:
        try:
            payload = json.loads(cleaned)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        payload = json.loads(match.group(0))
        if isinstance(payload, dict):
            return payload
    preview = text[:400].replace("\n", " ")
    _die(f"Error: could not parse JSON from Gemini. Preview: {preview!r}")


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower().strip())
    return s.strip("-")[:64] or "inst"


def main() -> None:
    print("Started")
    load_dotenv(ROOT / ".env")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        _die("Error: set GEMINI_API_KEY or GOOGLE_API_KEY in .env")

    phase1 = json.loads(PHASE1_JSON.read_text(encoding="utf-8"))
    sample = phase1.get("tier_college_sample") or []
    sb = phase1.get("student_background") or {}
    ri = phase1.get("research_interests") or {}

    prompt = f"""Select up to {N} PhD target institutes for this student using Google Search.
Return JSON: {{ "institutes": [ ... ] }}

Each institute:
- matched_whitelist_name: one name from the whitelist
- name, primary_domain (hostname only)
- official_source_urls: array of HTTPS URLs
- department_or_lab_urls: array of HTTPS URLs
- seed_profiles: optional array of {{ name, role, source_urls }}

Whitelist:
{json.dumps(sample, ensure_ascii=False, indent=2)}

Student background:
{json.dumps(sb, ensure_ascii=False)}

Research interests:
{json.dumps(ri, ensure_ascii=False)}

department_focus: {json.dumps(phase1.get("department_focus"))}
lab_focus: {json.dumps(phase1.get("lab_focus"))}
"""

    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )

    text = _response_text(response)
    if not text:
        _die("Error: empty response from Gemini.")

    payload = _parse_json_payload(text)
    institutes = payload.get("institutes") or []

    out_rows = []
    for i, inst in enumerate(institutes[:N]):
        if not isinstance(inst, dict):
            continue
        name = (inst.get("name") or inst.get("matched_whitelist_name") or f"institute-{i}").strip()
        dom = (inst.get("primary_domain") or "").strip().lower().rstrip(".")
        row = {
            "institute_id": (inst.get("institute_id") or "").strip() or f"{_slug(name)}-{_slug(dom)}-{i}",
            "name": name,
            "matched_whitelist_name": inst.get("matched_whitelist_name"),
            "primary_domain": dom,
            "official_source_urls": inst.get("official_source_urls") or [],
            "department_or_lab_urls": inst.get("department_or_lab_urls") or [],
        }
        if inst.get("seed_profiles"):
            row["seed_profiles"] = inst["seed_profiles"]
        if inst.get("notes"):
            row["notes"] = inst["notes"]
        out_rows.append(row)

    out = {"phase": 2, "gemini_model": GEMINI_MODEL, "institutes": out_rows}
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
