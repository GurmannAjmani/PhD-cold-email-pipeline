from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq, BadRequestError

ROOT = Path(__file__).resolve().parent
GROQ_MODEL = "llama-3.3-70b-versatile"
EMAIL_PLACEHOLDER = "[not found — please fill]"

FEEDBACK_JSON = ROOT / "output" / "feedback_store.json"

PHASE4_JSON = ROOT / "output" / "phase4_candidates_enriched.json"
PROFILE_TXT = ROOT / "samples" / "profile.txt"
RESEARCH_TXT = ROOT / "samples" / "research_interests.txt"
MAIL_DIR = ROOT / "output" / "mails"


def _norm_name(name: str) -> str:
    s = re.sub(
        r"^(?:(?:prof\.?|dr\.?|mr\.?|mrs\.?|ms\.?)\s*)+",
        "",
        name.strip(),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", s).strip().lower()


def _load_feedback() -> dict:
    if not FEEDBACK_JSON.is_file():
        return {"global_drafting": [], "professors": {}}
    data = json.loads(FEEDBACK_JSON.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"global_drafting": [], "professors": {}}
    data.setdefault("global_drafting", [])
    data.setdefault("professors", {})
    return data


def _raw_lines(records: list) -> str:
    lines: list[str] = []
    for rec in records:
        if isinstance(rec, dict) and (rec.get("text") or "").strip():
            lines.append(rec["text"].strip())
    return "\n".join(lines)


def _global_feedback_text(store: dict) -> str:
    return _raw_lines(store.get("global_drafting") or [])


def _prof_feedback_text(store: dict, name: str) -> str:
    target = _norm_name(name)
    for key, records in (store.get("professors") or {}).items():
        if _norm_name(key) == target:
            return _raw_lines(records if isinstance(records, list) else [])
    return ""


def _all_prof_feedback_text(store: dict) -> str:
    lines: list[str] = []
    for key, records in (store.get("professors") or {}).items():
        text = _raw_lines(records if isinstance(records, list) else [])
        if text:
            lines.append(f"{key}:\n{text}")
    return "\n\n".join(lines)


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _slug(s: str) -> str:
    t = re.sub(r"[^a-z0-9]+", "_", s.strip().lower())
    return (t.strip("_")[:72] or "prof").strip("_")


def _strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _flatten_prose(val: object) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return ""
        if s[0] in "{[":
            try:
                return _flatten_prose(ast.literal_eval(s))
            except (ValueError, SyntaxError):
                pass
        return s
    if isinstance(val, dict):
        parts: list[str] = []
        for v in val.values():
            chunk = _flatten_prose(v)
            if chunk:
                parts.append(chunk)
        return "\n\n".join(parts)
    if isinstance(val, list):
        parts = [_flatten_prose(item) for item in val]
        return "\n\n".join(p for p in parts if p)
    return str(val).strip()


def _as_text(val: object, fallback: str = "") -> str:
    text = _flatten_prose(val)
    return text if text else fallback


def _signature(profile: str) -> str:
    name_m = re.search(r"^Name:\s*(.+)$", profile, re.MULTILINE | re.IGNORECASE)
    phone_m = re.search(r"^Phone:\s*(.+)$", profile, re.MULTILINE | re.IGNORECASE)
    name = name_m.group(1).strip() if name_m else "Student"
    phone = phone_m.group(1).strip() if phone_m else ""
    lines = ["Regards,", name]
    if phone:
        lines.append(phone)
    return "\n".join(lines)


def _ensure_signature(body: str, profile: str) -> str:
    body = body.strip()
    if re.search(r"\bregards\b", body, re.IGNORECASE):
        return body
    return f"{body}\n\n{_signature(profile)}"


def _parse_draft(text: str) -> tuple[str, str]:
    raw = _strip_json_fences(text)

    if re.search(r"^SUBJECT:", raw, re.IGNORECASE | re.MULTILINE):
        parts = re.split(r"^BODY:\s*", raw, maxsplit=1, flags=re.IGNORECASE | re.MULTILINE)
        if len(parts) == 2:
            subject = re.sub(r"^SUBJECT:\s*", "", parts[0], flags=re.IGNORECASE).strip()
            body = parts[1].strip()
            if subject and body:
                return subject, body

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return _as_text(data.get("subject"), "PhD inquiry"), _as_text(data.get("body"))
    except json.JSONDecodeError:
        pass

    sub_m = re.search(r'"subject"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL | re.IGNORECASE)
    body_m = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL | re.IGNORECASE)
    if sub_m and body_m:
        return sub_m.group(1).strip(), body_m.group(1).strip()

    sub_m = re.search(r'"subject"\s*:\s*"([^"]*)"', raw, re.IGNORECASE)
    body_m = re.search(r'"body"\s*:\s*\n?(.*)$', raw, re.DOTALL | re.IGNORECASE)
    if sub_m and body_m:
        body = body_m.group(1).strip().rstrip("}").strip().strip("`")
        return sub_m.group(1).strip(), body

    _die(f"Error: could not parse Groq draft.\n---\n{raw[:2000]}\n---")


def _groq_draft(client: Groq, prompt: str) -> tuple[str, str]:
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.55,
            max_tokens=2048,
        )
        return _parse_draft(resp.choices[0].message.content or "")
    except BadRequestError as e:
        err = getattr(e, "body", None) or {}
        if isinstance(err, dict):
            failed = (err.get("error") or {}).get("failed_generation")
            if failed:
                return _parse_draft(str(failed))
        raise


def _professor_context(row: dict) -> str:
    lines: list[str] = []
    if row.get("lab_name"):
        lines.append(f"Lab: {row['lab_name']}")
    snippet = (row.get("research_interests_snippet") or "").strip()
    if snippet:
        lines.append(f"Research focus: {snippet[:1500]}")
    for p in (row.get("papers") or [])[:6]:
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        if not title:
            continue
        year = p.get("year")
        year_s = f" ({year})" if year else ""
        snip = (p.get("snippet") or "").strip()[:350]
        block = f"- {title}{year_s}"
        if snip:
            block += f"\n  Snippet: {snip}"
        lines.append(block)
    return "\n".join(lines) if lines else "(no extra context)"


def main() -> None:
    print("Phase 5 started")
    load_dotenv(ROOT / ".env")
    key = os.getenv("GROQ_API_KEY")
    if not key:
        _die("Error: set GROQ_API_KEY in .env")

    phase4 = json.loads(PHASE4_JSON.read_text(encoding="utf-8"))
    profile = PROFILE_TXT.read_text(encoding="utf-8")
    research = RESEARCH_TXT.read_text(encoding="utf-8")
    store = _load_feedback()
    enriched = phase4.get("candidates_enriched") or []
    global_fb = _global_feedback_text(store)
    all_prof_fb = _all_prof_feedback_text(store)

    client = Groq(api_key=key)
    MAIL_DIR.mkdir(parents=True, exist_ok=True)

    for idx, row in enumerate(enriched, start=1):
        name = (row.get("name") or "Professor").strip()
        institute = (row.get("institute") or "").strip()
        lab = (row.get("lab_name") or "").strip()
        prof_ctx = _professor_context(row)
        prof_fb = _prof_feedback_text(store, name)
        to_line = (row.get("email") or "").strip() or EMAIL_PLACEHOLDER

        global_block = ""
        if global_fb:
            global_block = f"\nUser feedback on how to draft emails (follow this):\n{global_fb}\n"

        prof_history_block = ""
        if all_prof_fb:
            prof_history_block = (
                "\nOutreach history for all professors (use to judge priority and tone — "
                "e.g. deprioritize if user says no reply):\n"
                f"{all_prof_fb}\n"
            )

        this_prof_block = ""
        if prof_fb:
            this_prof_block = f"\nUser notes specifically about {name}:\n{prof_fb}\n"

        print(f"  [{idx}/{len(enriched)}] {name!r}")

        prompt = f"""Write a personalized formal PhD inquiry email. Use exactly this format (plain text, not JSON):

SUBJECT: <subject line, at most 20 words>

BODY:
<3 or 4 short paragraphs separated by blank lines>

Content requirements (be specific, not generic):
1. Paragraph 1: Greet the professor. Say why you are writing to them specifically — name their lab ({lab or "their group"}) and a concrete research theme from their work below.
2. Paragraph 2: Discuss their prior work in detail — cite at least one paper title OR lab theme from the professor context below and say what you found interesting about it (use the snippets; do not invent papers not listed).
3. Paragraph 3: Describe your own work in detail — name at least two specific projects or experiences from the student CV (methods, datasets, goals) and explain how they connect to the professor's research (not just "our interests align").
4. Final lines: Briefly ask about PhD opportunities, then close with "Regards," and the student's name and phone from the CV.
{global_block}{prof_history_block}{this_prof_block}
Professor: {name}
Institution: {institute}

Professor context (papers, lab, research):
{prof_ctx}

Student CV:
{profile[:3500]}

Student research interests:
{research[:2500]}
"""

        subject, body = _groq_draft(client, prompt)
        body = _ensure_signature(body, profile)

        fpath = MAIL_DIR / f"{_slug(name)}_{idx}_name.txt"
        fpath.write_text(f"To: {to_line}\n\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
        print(f"    Wrote {fpath.relative_to(ROOT)}")

        if idx < len(enriched):
            time.sleep(0.4)

    print(f"Phase 5 finished ({len(enriched)} files)")


if __name__ == "__main__":
    main()
