"""
VC portfolio board aggregator.

Renders each VC job board in a headless browser (Playwright), reads the
jobs that load on the page, filters them to the target role keywords and
AU/US locations, dedupes, and writes data/jobs.json for the app to read.

Rendering (rather than calling private APIs) makes this platform-agnostic:
it works for Consider, Getro, or anything else, because it reads what the
page actually shows. Each board also gets a debug/<firm>.html snapshot so
any first-run selector tuning is quick.

Runs in CI (GitHub Actions). No secrets required.
"""

import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DATA = ROOT / "data"
DEBUG = ROOT / "debug"

NAV_TIMEOUT = 45_000      # ms to load a board
SCROLL_ROUNDS = 25        # how many times to scroll to pull in lazy-loaded jobs
SCROLL_PAUSE = 900        # ms between scrolls
MAX_PER_FIRM = 1500       # safety cap on raw cards pulled per board


# ---------------------------------------------------------------- filters

def load_json(p):
    return json.loads(Path(p).read_text())


FILTERS = load_json(SCRIPTS / "filters.json")
INCLUDE = [k.lower() for k in FILTERS["include"]]
EXCLUDE = [k.lower() for k in FILTERS["exclude"]]
COS_MARKERS = [k.lower() for k in FILTERS["cos_markers"]]


def relevant(title):
    t = title.lower()
    return any(k in t for k in INCLUDE) and not any(k in t for k in EXCLUDE)


def is_cos(title):
    t = title.lower()
    return any(k in t for k in COS_MARKERS)


# Region filter — same logic as the app's inRegion(), ported to Python.
AU_RE = re.compile(r"\b(australia|sydney|melbourne|brisbane|perth|canberra|adelaide|gold coast|newcastle|nsw|qld|apac)\b")
USW_RE = re.compile(r"\b(united states|u\.?s\.?a\.?|usa|americas?)\b")
USST_RE = re.compile(r",\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)\b")
USCI_RE = re.compile(r"\b(san francisco|sf bay|bay area|new york|nyc|brooklyn|seattle|boston|cambridge|austin|chicago|los angeles|palo alto|mountain view|menlo park|sunnyvale|san jose|santa clara|washington|d\.?c\.?|denver|boulder|atlanta|miami|dallas|houston|san diego|bellevue|redmond|philadelphia|pittsburgh|portland|nashville|phoenix|minneapolis|detroit|raleigh|durham|kirkland|salt lake city)\b")
OTHER_RE = re.compile(r"\b(london|united kingdom|u\.?k\.?|england|scotland|wales|ireland|dublin|stockholm|sweden|oslo|norway|copenhagen|denmark|helsinki|finland|germany|berlin|munich|hamburg|france|paris|netherlands|amsterdam|spain|madrid|barcelona|italy|milan|rome|portugal|lisbon|poland|warsaw|zurich|switzerland|geneva|brussels|belgium|vienna|austria|czech|prague|romania|bucharest|greece|athens|singapore|india|bangalore|bengaluru|hyderabad|mumbai|delhi|gurgaon|gurugram|pune|chennai|noida|japan|tokyo|osaka|china|beijing|shanghai|shenzhen|hong kong|korea|seoul|taiwan|taipei|philippines|manila|indonesia|jakarta|vietnam|hanoi|thailand|bangkok|malaysia|kuala lumpur|canada|toronto|vancouver|montreal|ottawa|calgary|waterloo|brazil|sao paulo|mexico|guadalajara|argentina|buenos aires|colombia|bogota|chile|santiago|peru|lima|dubai|u\.?a\.?e|abu dhabi|qatar|doha|saudi|riyadh|israel|tel aviv|turkey|istanbul|south africa|cape town|johannesburg|nigeria|lagos|kenya|nairobi|egypt|cairo|new zealand|auckland|wellington|emea|latam)\b")


def in_region(loc):
    raw = (loc or "").lower().strip()
    if not raw:
        return True
    segs = [s.strip() for s in re.split(r"[;/|\n]| or | & ", raw) if s.strip()] or [raw]
    any_region = any_other = any_unknown = False
    for s in segs:
        au = bool(AU_RE.search(s))
        us = bool(USW_RE.search(s) or USST_RE.search(s) or USCI_RE.search(s))
        if au or us:
            any_region = True
        elif OTHER_RE.search(s):
            any_other = True
        else:
            any_unknown = True
    if any_region:
        return True
    if any_other and not any_unknown:
        return False
    return True


# ---------------------------------------------------------------- extraction

# Runs in the page context. Consider/Getro render each role as a link whose
# href points at a job or company/job detail. We collect those links plus a
# little surrounding text, and parse title/company/location out of it. Kept
# deliberately generic so it survives markup changes and works across boards.
EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const longestLine = (s) => (s || '').split('\n').map(x => x.trim())
      .filter(Boolean).sort((a, b) => b.length - a.length)[0] || '';
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  for (const a of anchors) {
    const href = a.getAttribute('href') || '';
    // job-detail links across Consider/Getro look like /jobs/... or /companies/.../jobs/...
    if (!/\/jobs?\//i.test(href) && !/\/companies\/[^/]+\/[^/]+/i.test(href)) continue;
    // Climb to the card container: go up while the ancestor still wraps only
    // this one job link. Stop before an ancestor that groups sibling cards,
    // so context stays scoped to a single role (cap the climb for safety).
    const isJobLink = (el) => Array.from(el.querySelectorAll('a[href]'))
      .filter(x => /\/jobs?\//i.test(x.getAttribute('href') || '') ||
                   /\/companies\/[^/]+\/[^/]+/i.test(x.getAttribute('href') || '')).length;
    let card = a;
    for (let i = 0; i < 6 && card.parentElement; i++) {
      if (isJobLink(card.parentElement) > 1) break;
      card = card.parentElement;
    }
    // title: prefer a heading element (Consider/Getro put the role in one),
    // then the anchor's aria-label, then the longest line of anchor text.
    const h = a.querySelector('h1,h2,h3,h4,[role="heading"]') ||
              card.querySelector('h1,h2,h3,h4,[role="heading"]');
    let title = h ? h.innerText.trim()
              : (a.getAttribute('aria-label') || '').trim()
              || longestLine(a.innerText);
    title = (title || '').split('\n')[0].trim();
    if (!title || title.length < 3) continue;
    const ctx = (card.innerText || '').replace(/\s+\n/g, '\n').trim();
    const abs = href.startsWith('http') ? href : (location.origin + href);
    const key = abs + '::' + title;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ title, url: abs, context: ctx });
  }
  return out;
}
"""

LOCATION_HINT = re.compile(
    r"(remote|hybrid|on-?site|[A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2}\b|"
    r"san francisco|new york|london|sydney|melbourne|singapore|berlin|paris|"
    r"stockholm|boston|austin|seattle|toronto|bangalore|tel aviv|dublin)",
    re.IGNORECASE,
)


def parse_card(firm, entry):
    """Turn a raw {title,url,context} into a normalized job dict, or None."""
    title = entry["title"].strip()
    if not relevant(title):
        return None

    # Best-effort company + location from the card's text block.
    lines = [l.strip() for l in entry.get("context", "").split("\n") if l.strip()]
    company = ""
    location = ""
    if title in lines:
        idx = lines.index(title)
        # company usually sits just above the title on Consider cards
        if idx > 0:
            company = lines[idx - 1]
        # location usually within a couple of lines below
        for l in lines[idx + 1: idx + 4]:
            if LOCATION_HINT.search(l):
                location = l
                break
    if not location:
        for l in lines:
            if LOCATION_HINT.search(l) and l != title and l != company:
                location = l
                break

    company = re.sub(r"\s{2,}", " ", company)[:80]
    location = re.sub(r"\s{2,}", " ", location)[:120]

    if not in_region(location):
        return None

    return {
        "firm": firm,
        "company": company or firm,
        "title": title,
        "location": location,
        "url": entry["url"],
        "cos": is_cos(title),
        "id": f"vc:{firm}:{entry['url']}",
    }


def scrape_board(page, board):
    firm, url = board["firm"], board["url"]
    print(f"  -> {firm}: {url}")
    page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    # Scroll to pull lazily-loaded roles; stop early once the count plateaus.
    last = 0
    stable = 0
    for _ in range(SCROLL_ROUNDS):
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(SCROLL_PAUSE)
        try:
            count = page.evaluate("() => document.querySelectorAll('a[href]').length")
        except Exception:
            count = last
        if count <= last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = count

    # Save a debug snapshot regardless of outcome.
    try:
        DEBUG.mkdir(exist_ok=True)
        (DEBUG / f"{firm.replace(' ', '_')}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass

    raw = page.evaluate(EXTRACT_JS)[:MAX_PER_FIRM]
    jobs = []
    for entry in raw:
        job = parse_card(firm, entry)
        if job:
            jobs.append(job)
    # de-dupe within a firm by url
    uniq = {}
    for j in jobs:
        uniq[j["url"]] = j
    print(f"     {len(raw)} cards seen, {len(uniq)} relevant AU/US roles")
    return list(uniq.values())


# ---------------------------------------------------------------- main

def main():
    boards = load_json(SCRIPTS / "boards.json")["boards"]
    all_jobs = []
    failures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
            viewport={"width": 1280, "height": 1800},
        )
        for board in boards:
            page = ctx.new_page()
            try:
                all_jobs.extend(scrape_board(page, board))
            except Exception as e:
                failures.append({"firm": board["firm"], "error": str(e)})
                print(f"     FAILED: {e}")
                traceback.print_exc()
            finally:
                page.close()
        browser.close()

    # Global de-dupe across firms: the same company role can appear on
    # several VC boards. Key on company+title, keep the first firm seen.
    seen = set()
    deduped = []
    for j in sorted(all_jobs, key=lambda x: (x["company"].lower(), x["title"].lower())):
        key = (j["company"].lower().strip(), j["title"].lower().strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(j)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(deduped),
        "firms": [b["firm"] for b in boards],
        "failures": failures,
        "jobs": deduped,
    }
    DATA.mkdir(exist_ok=True)
    (DATA / "jobs.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {len(deduped)} roles from {len(boards) - len(failures)}/{len(boards)} boards "
          f"to data/jobs.json ({len(failures)} failed).")

    # Non-zero exit only if EVERY board failed (keeps the schedule healthy
    # when just one board changes markup).
    if failures and len(failures) == len(boards):
        sys.exit("All boards failed — see logs and debug/ snapshots.")


if __name__ == "__main__":
    main()
