from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
GEMINI_MODEL = "gemini-2.5-flash"
MAX_CANDIDATES = 10

FEEDBACK_JSON = ROOT / "output" / "feedback_store.json"
PHASE1_JSON = ROOT / "output" / "student_output.json"
PHASE2_JSON = ROOT / "output" / "phase2_institutes.json"
OUTPUT_JSON = ROOT / "output" / "phase3_candidates.json"


def _load_feedback() -> dict:
    if not FEEDBACK_JSON.is_file():
        return {"global_drafting": [], "professors": {}}
    data = json.loads(FEEDBACK_JSON.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"global_drafting": [], "professors": {}}
    data.setdefault("global_drafting", [])
    data.setdefault("professors", {})
    return data


def _feedback_block(store: dict) -> str:
    lines: list[str] = []
    for rec in store.get("global_drafting") or []:
        if isinstance(rec, dict) and (rec.get("text") or "").strip():
            lines.append(rec["text"].strip())
    for key, records in (store.get("professors") or {}).items():
        for rec in records if isinstance(records, list) else []:
            if isinstance(rec, dict) and (rec.get("text") or "").strip():
                lines.append(f"{key}: {rec['text'].strip()}")
    if not lines:
        return ""
    return (
        "\nUser feedback (prefer or avoid supervisors accordingly; "
        "replace poor fits with better matches):\n"
        + "\n".join(lines)
        + "\n"
    )


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
    regular: list[str] = []
    thought: list[str] = []
    for cand in getattr(response, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            t = getattr(part, "text", None)
            if not t:
                continue
            if getattr(part, "thought", False):
                thought.append(t)
            else:
                regular.append(t)
    return ("\n".join(regular) or "\n".join(thought)).strip()


def _response_detail(response) -> str:
    pf = getattr(response, "prompt_feedback", None)
    if pf and getattr(pf, "block_reason", None):
        return f"prompt blocked: {pf.block_reason}"
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return "no candidates returned"
    c0 = cands[0]
    return (
        f"finish_reason={getattr(c0, 'finish_reason', None)!r} "
        f"finish_message={getattr(c0, 'finish_message', None)!r}"
    )


def _gemini_json(client, prompt: str) -> dict:
    configs = (
        types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
        types.GenerateContentConfig(temperature=0.2),
    )
    last_detail = "unknown"
    for attempt, config in enumerate(configs, start=1):
        for retry in range(2):
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            text = _response_text(response)
            if text:
                return _parse_json_payload(text)
            last_detail = _response_detail(response)
            print(
                f"Empty Gemini response (attempt {attempt}, retry {retry + 1}): {last_detail}",
                flush=True,
            )
            time.sleep(2)
    _die(f"Error: empty response from Gemini. {last_detail}")


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


def main() -> None:
    print("Started")
    load_dotenv(ROOT / ".env")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        _die("Error: set GEMINI_API_KEY or GOOGLE_API_KEY in .env")

    phase1 = json.loads(PHASE1_JSON.read_text(encoding="utf-8"))
    phase2 = json.loads(PHASE2_JSON.read_text(encoding="utf-8"))
    institutes = phase2.get("institutes") or []
    feedback = _feedback_block(_load_feedback())

    prompt = f"""Find up to {MAX_CANDIDATES} PhD supervisors (faculty or lab PIs) for this student using Google Search.
Return ONLY valid JSON with no preamble: {{ "candidates": [ ... ] }}

Each candidate:
- name, role (faculty|lab_pi), lab_name (or null)
- institute, institute_id, primary_domain
- source_urls: array of URLs
- email: official work email or null
- research_interests_snippet
- confidence: 0-1

Institutes:
{json.dumps(institutes, ensure_ascii=False, indent=2)}

Student background:
{json.dumps(phase1.get("student_background"), ensure_ascii=False)}

Research interests:
{json.dumps(phase1.get("research_interests"), ensure_ascii=False)}
{feedback}"""

    client = genai.Client(api_key=key)
    payload = _gemini_json(client, prompt)
    candidates = (payload.get("candidates") or [])[:MAX_CANDIDATES]

    out = {"candidates": candidates}
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_JSON} ({len(candidates)} candidates)")


if __name__ == "__main__":
    main()
