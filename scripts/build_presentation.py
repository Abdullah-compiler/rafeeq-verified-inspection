# /// script
# requires-python = ">=3.12"
# dependencies = ["reportlab>=4.2", "pypdfium2>=4.30", "pillow>=10.4"]
# ///
"""Build original slides and an honest replay from this project's actual evidence."""
from __future__ import annotations

import argparse
import csv
import html
import json
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def evaluation_rows(config):
    options = [ROOT / "results/evaluation" / f"eval_{config}.csv", ROOT / "results/evaluation" / f"{config}.csv"]
    path = next((p for p in options if p.exists()), None)
    return list(csv.DictReader(path.open(encoding="utf-8", newline=""))) if path else []


def passed(row):
    return str(row.get("passed", row.get("pass", ""))).lower() in {"true", "1", "yes"}


def collect(allow_partial=False):
    task_file = ROOT / "labs/capstone/tasks.jsonl"
    tasks = [json.loads(line) for line in task_file.read_text().splitlines() if line.strip()] if task_file.exists() else []
    rows = {config: evaluation_rows(config) for config in ("A", "B")}
    count = len(tasks)
    summary = load_json(ROOT / "results/evaluation/summary.json")
    complete = count >= 15 and summary.get("complete") is True and all(len(rows[c]) == count and all(r.get("status") == "completed" for r in rows[c]) for c in rows)
    if not complete and not allow_partial:
        raise RuntimeError("Full A/B CSVs are required; use --allow-partial only to label an incomplete presentation honestly.")
    tests = load_json(ROOT / "results/offline-tests.json")
    security = load_json(ROOT / "results/security/summary.json")
    totals = {c: {"count": sum(x.get("status") == "completed" for x in r), "passed": sum(x.get("status") == "completed" and passed(x) for x in r)} for c, r in rows.items()}
    return {"tasks": count, "complete": complete, "totals": totals, "tests": tests, "security": security, "rows": rows, "summary": summary}


def slide_content(data):
    scores = []
    for config in ("A", "B"):
        t = data["totals"][config]
        rate = f"{100 * t['passed'] / t['count']:.1f}%" if t["count"] else "not run"
        scores.append(f"{config}: {t['passed']}/{t['count']} completed tasks passed ({rate}); {t['count']}/{data['tasks']} runs complete")
    security_rows = data["security"].get("results", [])
    security_line = "Security experiment not completed"
    if len(security_rows) == 2 and all(r.get("completed") for r in security_rows):
        security_line = f"Sensitive outbox rows: {security_rows[0]['sensitive_outbox_rows']} unsafe -> {security_rows[1]['sensitive_outbox_rows']} defended"
    failures = []
    for config, rows in data["rows"].items():
        for row in rows:
            if row.get("status") == "completed" and not passed(row):
                ident = row.get("id", row.get("task_id", "task"))
                reason = row.get("failure_category", row.get("root_cause", row.get("failures", row.get("failed_graders", row.get("error", "see raw result")))))
                failures.append(f"{config} / {ident}: {reason}"[:170])
    return [
        ("Rafeeq", "Verified Inspection Desk", ["Arabic / English municipal assistance", "The model proposes. The human decides. The database proves it.", "SDAIA Academy capstone | Track 1 | Synthetic data only"]),
        ("01 / The useful job", "One workflow, end to end", ["Verify the permit holder without revealing whether an unverified permit exists.", "Retrieve inspection policy over MCP; find actual district availability.", "Present an exact booking for human approval; commit only after revalidation.", "Demo fixture: FTR-30311 / health inspection / Al-Rawdah."]),
        ("02 / Architecture", "Keep authority outside the model", ["CLI or authenticated HTTP client -> bounded agent loop", "Public policy MCP -> retrieved rules, no applicant database tools", "Local permit + slot + booking tools -> one persistent session database", "Human approval -> authorization recheck -> atomic write -> read-back"]),
        ("03 / Controls", "A prompt is not a permission system", ["Four tools only; unknown capabilities are unavailable.", "Identity, current ownership and agent authorization checked in code.", "Denied approval stays denied for that turn; exact slot rechecked at commit.", "Bounded history/results, step and cost limits; redacted evidence.", f"Offline verification: {data['tests'].get('tests_run', 'not run')} checks; full evidence in results/."]),
        ("04 / Evaluation", "Change one thing; measure honestly", [f"Suite: {data['tasks']} original tasks; two configurations.", "A: generated final reply. B: verified booking/denial renderer. Safety unchanged.", *scores, f"Authentic cache replays: A {data['summary'].get('configs', {}).get('A', {}).get('cache_hits', 0)}, B {data['summary'].get('configs', {}).get('B', {}).get('cache_hits', 0)}. Real tools, fresh DBs; not independent sampling.", "Full suite complete; scripted synthetic consent, not human sign-off." if data["complete"] else "INCOMPLETE: provider/quota or missing runs remain; do not present as a completed evaluation."]),
        ("05 / Error analysis", "Failures are part of the deliverable", (["A loses booking confirmation fields; B fixes two paired outcomes.", "Health denial task: requested slot unavailable; no denial gate exercised.", "Refund answer invents a refund-request capability; no write occurred.", "Arabic needs-info refusal trips a lexical negation false positive."] if data["complete"] else (failures[:4] or ["No failed rows in the available CSVs; this small sample does not prove general reliability."])) + ["Scores and original graders retained; full analysis in docs/EVALUATION.md.", "Policy/refusal prose remains model-generated; rendering is not better planning.", "No pass^3 or production-readiness claim."]),
        ("06 / Security", "Break a capability chain, not just a phrase", ["Private records + untrusted ticket + outbound channel form the trifecta.", "A false audit override requested full identity fields to an attacker address.", security_line, "The control is an isolated unsafe lab; the defended run uses our actual app.", "Only a local synthetic outbox is used. No real email. One attack is not comprehensive assurance."]),
        ("07 / Handover", "Reproduce, inspect, question", ["uv run rafeeq.py checks", "uv run rafeeq.py chat --session demo", "uv run rafeeq.py serve --host 127.0.0.1 --port 8793", "README + tasks + A/B CSVs + security evidence + slides.", "Limits: synthetic identity, isolated session databases, free-provider availability, heuristic PII masking."]),
    ]


def build_pdf(slides, output, incomplete=False):
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import HexColor
    c = canvas.Canvas(str(output), pagesize=(1280, 720))
    c.setTitle("Rafeeq — Verified Inspection Desk")
    c.setAuthor(load_json(ROOT / "docs/publication.json").get("author", "Rafeeq capstone project"))
    for number, (kicker, title, bullets) in enumerate(slides, 1):
        c.setFillColor(HexColor("#101F2A")); c.rect(0, 0, 1280, 720, fill=1, stroke=0)
        c.setFillColor(HexColor("#50D6BC")); c.rect(60, 632, 52, 5, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 16); c.drawString(60, 665, kicker.upper())
        c.setFillColor(HexColor("#F4F1E8")); c.setFont("Helvetica-Bold", 44)
        c.drawString(60, 565, title)
        y = 470
        size = 19 if len(bullets) > 5 else 23
        leading, gap = (27, 14) if size == 19 else (33, 19)
        wrapped = [textwrap.wrap(bullet, width=106 if size == 19 else 88) for bullet in bullets]
        if sum(len(lines) * leading + gap for lines in wrapped) > 430:
            raise RuntimeError(f"Slide {number} text would overflow; shorten it before export.")
        for lines in wrapped:
            c.setFillColor(HexColor("#50D6BC")); c.circle(67, y + 6, 4, fill=1, stroke=0)
            c.setFillColor(HexColor("#D8E0E4")); c.setFont("Helvetica", size)
            for line in lines:
                c.drawString(88, y, line); y -= leading
            y -= gap
        c.setFillColor(HexColor("#8BA2AE")); c.setFont("Helvetica", 12)
        c.drawString(60, 32, "DRAFT / EVALUATION IN PROGRESS" if incomplete else "RAFEEQ / SYNTHETIC MUNICIPAL SERVICES / EVIDENCE, NOT PROMISES")
        c.drawRightString(1220, 32, f"{number:02d} / {len(slides):02d}")
        c.showPage()
    c.save()


def build_html(slides, output):
    sections = []
    for i, (k, title, bullets) in enumerate(slides):
        items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
        sections.append(f'<section class="slide" {"hidden" if i else ""}><p class="kicker">{html.escape(k)}</p><h1>{html.escape(title)}</h1><ul>{items}</ul></section>')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rafeeq / presentation</title><style>
*{box-sizing:border-box}body{margin:0;background:#101f2a;color:#f4f1e8;font-family:system-ui,sans-serif}.slide{min-height:100vh;padding:7vh 7vw 11vh}.kicker{color:#50d6bc;letter-spacing:.16em;text-transform:uppercase;font-weight:700}h1{font-size:clamp(32px,4vw,64px);letter-spacing:-.04em;margin:6vh 0}ul{max-width:1100px;padding-left:1.1em}li{font-size:clamp(18px,2vw,30px);line-height:1.45;margin:.65em 0}li::marker{color:#50d6bc}nav{position:fixed;bottom:24px;left:7vw;right:7vw;display:flex;align-items:center;gap:14px;color:#91a7b1}button{border:1px solid #40535e;background:none;color:#e3e9e9;padding:10px 18px;cursor:pointer;border-radius:6px}#count{margin-left:auto}
</style>''' + "".join(sections) + '''<nav><button onclick="step(-1)">Previous</button><button onclick="step(1)">Next</button><span>Arrow keys / Space</span><span id="count"></span></nav><script>let i=0;const slides=[...document.querySelectorAll('.slide')];function show(){slides.forEach((s,j)=>s.hidden=j!==i);document.querySelector('#count').textContent=`${i+1} / ${slides.length}`}function step(d){i=Math.max(0,Math.min(slides.length-1,i+d));show()}addEventListener('keydown',e=>{if(['ArrowRight',' '].includes(e.key)){e.preventDefault();step(1)}if(e.key==='ArrowLeft')step(-1)});show();</script></html>'''
    output.write_text(page, encoding="utf-8")


def build_replay(output):
    cases = {}
    for action in ("approve", "reject"):
        path = ROOT / "results" / f"booking-{action}-reviewed.json"
        if not path.exists():
            continue
        report = load_json(path)
        cases[action] = {"events": [e for e in report.get("trace", []) if e.get("kind") in {"call", "result", "guard", "error"}], "final": report["final"], "steps": report["steps"], "cost": report["reported_cost_usd"]}
    payload = json.dumps(cases, ensure_ascii=False).replace("</", "<\\/")
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rafeeq / recorded evidence replay</title><style>*{box-sizing:border-box}body{margin:0;background:#101f2a;color:#e7eeed;font-family:system-ui,sans-serif;padding:32px}main{max-width:1120px;margin:auto}h1{font-size:40px;letter-spacing:-.04em}p{color:#b3c4ca;line-height:1.6}.pill{color:#50d6bc;font-weight:700;text-transform:uppercase;letter-spacing:.1em}button,select{background:#1e3442;color:#f0f5f3;border:1px solid #4c636c;padding:12px 18px;border-radius:8px;margin-right:8px}.event{border-left:3px solid #50d6bc;background:#172b37;padding:18px;margin:12px 0;border-radius:8px}.kind{font-size:12px;color:#50d6bc;font-weight:700;text-transform:uppercase}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.6 ui-monospace,monospace}.final{border:1px solid #50d6bc;padding:20px;border-radius:10px}</style><main><p class="pill">Recorded evidence / not a live model session</p><h1>Rafeeq inspection desk</h1><p>This page replays redacted traces from real model-backed CLI tests. It makes no API requests and performs no bookings. The original approval inputs were scripted test inputs.</p><select id="case"><option value="approve">English / approval accepted</option><option value="reject">Arabic / approval rejected</option></select><button onclick="play()">Play trace</button><button onclick="showAll()">Show all</button><div id="events"></div><div class="final" id="final" hidden></div></main><script>const CASES=PAYLOAD;let timer=null;function chosen(){return CASES[document.querySelector('#case').value]}function clear(){clearInterval(timer);document.querySelector('#events').replaceChildren();document.querySelector('#final').hidden=true}function append(e){const box=document.createElement('div');box.className='event';const label=document.createElement('div');label.className='kind';label.textContent=e.kind;const text=document.createElement('pre');text.textContent=e.text;box.append(label,text);document.querySelector('#events').append(box)}function finish(){const el=document.querySelector('#final');el.hidden=false;el.textContent=chosen().final;el.style.whiteSpace='pre-wrap'}function play(){clear();let i=0,events=chosen().events;timer=setInterval(()=>{if(i<events.length)append(events[i++]);else{clearInterval(timer);finish()}},1800)}function showAll(){clear();chosen().events.forEach(append);finish()}document.querySelector('#case').addEventListener('change',clear);</script></html>'''.replace("PAYLOAD", payload)
    output.write_text(page, encoding="utf-8")


def render_contact_sheet(pdf, output):
    import pypdfium2 as pdfium
    from PIL import Image
    doc = pdfium.PdfDocument(str(pdf))
    thumbs = [page.render(scale=0.4).to_pil().convert("RGB") for page in doc]
    width, height = thumbs[0].size
    sheet = Image.new("RGB", (width * 2, height * ((len(thumbs) + 1) // 2)), "#101f2a")
    for i, image in enumerate(thumbs):
        sheet.paste(image, ((i % 2) * width, (i // 2) * height))
    sheet.save(output)
    doc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    data = collect(args.allow_partial)
    slides = slide_content(data)
    docs = ROOT / "docs"; docs.mkdir(exist_ok=True)
    build_pdf(slides, docs / "slides.pdf", incomplete=not data["complete"])
    build_html(slides, docs / "slides.html")
    build_replay(docs / "demo-replay.html")
    render_contact_sheet(docs / "slides.pdf", docs / "slides-contact.png")
    print(json.dumps({"slides": len(slides), "evaluation_complete": data["complete"], "artifacts": ["docs/slides.pdf", "docs/slides.html", "docs/demo-replay.html", "docs/slides-contact.png"]}, indent=2))


if __name__ == "__main__":
    main()
