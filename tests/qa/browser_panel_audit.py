#!/usr/bin/env python3
from __future__ import annotations
import json, os, re
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
RENDERED = Path(os.environ.get("FARGOVPN_RENDERED_DIR", "/mnt/data/rendered512"))
PAGES = sorted(p.stem for p in RENDERED.glob("*.html"))

MOCK_SCRIPT = r'''<script>
window.fetch = (url, opts) => Promise.resolve({
  ok:true, status:200,
  headers:new Headers({'content-type':'application/json'}),
  json:async()=>({ok:true,detail:'QA mock',available:false,text:'QA log',items:[],state:'idle',progress:0,next_after_id:0,dialogs:[],selected_events:[],unread:{total:0,items:[]}}),
  text:async()=>''
});
class QA_XHR {
  constructor(){this.upload={};this.status=200;this.responseText='{"ok":true,"status":{"state":"queued","progress":2,"version":"5.1.19"}}';}
  open(m,u){this.method=m;this.url=u;}
  setRequestHeader(){}
  send(){if(this.upload.onprogress)this.upload.onprogress({lengthComputable:true,loaded:1,total:1});setTimeout(()=>this.onload&&this.onload(),10);}
}
window.XMLHttpRequest=QA_XHR;
window.confirm=()=>true;
</script>'''


def main() -> int:
    panel_js = (ROOT / "app" / "static" / "panel.js").read_text(encoding="utf-8")
    result = {"pages": {}, "total_buttons": 0, "button_pass": 0, "button_fail": 0, "total_selects": 0, "select_pass": 0, "select_fail": 0, "total_checkboxes": 0, "checkbox_pass": 0, "checkbox_fail": 0, "console_errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        context = browser.new_context(service_workers="block")
        for name in PAGES:
            page = context.new_page()
            errors, navs = [], []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.on("console", lambda msg, errors=errors: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None)
            page.on("framenavigated", lambda frame, navs=navs, page=page: navs.append(frame.url) if frame == page.main_frame else None)
            try:
                html = (RENDERED / f"{name}.html").read_text(encoding="utf-8")
                html = re.sub(r'<script src="/static/panel\.js\?[^\"]+" defer></script>', "<script>\n" + panel_js + "\n</script>", html, count=1)
                # Mocks must appear after panel.js and before page-specific scripts.
                first_close = html.find("</script>")
                html = html[:first_close + len("</script>")] + MOCK_SCRIPT + html[first_close + len("</script>"):]
                page.set_content(html, wait_until="domcontentloaded")
                page.wait_for_timeout(200)
                navs.clear(); errors.clear()
                buttons = page.locator('button[type="button"]')
                count = buttons.count()
                page_pass = 0; failures = []
                for i in range(count):
                    btn = buttons.nth(i)
                    try:
                        if not btn.is_visible() or not btn.is_enabled():
                            continue
                        label = (btn.inner_text(timeout=500) or btn.get_attribute("aria-label") or f"button-{i}").strip()[:100]
                        before = page.url
                        navs.clear(); errors.clear()
                        btn.click(timeout=1500)
                        page.wait_for_timeout(300)
                        if page.url != before or navs:
                            failures.append({"button":label,"reason":"unexpected navigation","url":page.url})
                        elif errors:
                            failures.append({"button":label,"reason":errors[-1]})
                        else:
                            page_pass += 1
                    except Exception as exc:
                        failures.append({"button":f"button-{i}","reason":str(exc)[:220]})
                result["total_buttons"] += count
                result["button_pass"] += page_pass
                result["button_fail"] += len(failures)

                select_failures=[]; select_pass=0
                selects=page.locator('select')
                for i in range(selects.count()):
                    el=selects.nth(i)
                    try:
                        if not el.is_visible() or el.is_disabled(): continue
                        options=el.locator('option')
                        if options.count()<2: continue
                        before=page.url; navs.clear(); errors.clear()
                        target=options.nth(1).get_attribute('value') or ''
                        el.select_option(target)
                        page.wait_for_timeout(120)
                        if page.url!=before or navs:
                            select_failures.append({"index":i,"reason":"unexpected navigation"})
                        elif errors:
                            select_failures.append({"index":i,"reason":errors[-1]})
                        else: select_pass+=1
                    except Exception as exc:
                        select_failures.append({"index":i,"reason":str(exc)[:180]})
                result["total_selects"] += selects.count()
                result["select_pass"] += select_pass
                result["select_fail"] += len(select_failures)

                cb_failures=[]; cb_pass=0
                cbs=page.locator('input[type="checkbox"]')
                for i in range(cbs.count()):
                    el=cbs.nth(i)
                    try:
                        if not el.is_visible() or el.is_disabled(): continue
                        before=page.url; navs.clear(); errors.clear()
                        el.click(); page.wait_for_timeout(100)
                        if page.url!=before or navs:
                            cb_failures.append({"index":i,"reason":"unexpected navigation"})
                        elif errors:
                            cb_failures.append({"index":i,"reason":errors[-1]})
                        else:
                            cb_pass+=1
                            el.click()
                    except Exception as exc:
                        cb_failures.append({"index":i,"reason":str(exc)[:180]})
                result["total_checkboxes"] += cbs.count()
                result["checkbox_pass"] += cb_pass
                result["checkbox_fail"] += len(cb_failures)

                result["console_errors"].extend({"page":name,"error":e} for e in errors)
                result["pages"][name] = {"buttons":count,"pass":page_pass,"fail":failures,"selects":selects.count(),"select_pass":select_pass,"select_fail":select_failures,"checkboxes":cbs.count(),"checkbox_pass":cb_pass,"checkbox_fail":cb_failures,"errors":errors}
            except Exception as exc:
                result["button_fail"] += 1
                result["pages"][name] = {"error":str(exc)}
            finally:
                page.close()
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(result[k] == 0 for k in ("button_fail","select_fail","checkbox_fail")) and not result["console_errors"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
