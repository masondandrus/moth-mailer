#!/usr/bin/env python3
"""
Publications tracker for mothsforanna.com.

Searches PubMed, bioRxiv, and Google Scholar (via SerpAPI) each week for new
papers on moths, nudibranchs, and amphibians, drops agricultural pest-control
papers, and appends the rest to publications/data/<year>.json.

Existing entries are never removed or rewritten. If any existing data file
fails to parse, the script exits without writing anything.

Usage:
    python publications_tracker.py               # search the last 7 days, write
    python publications_tracker.py --dry-run     # search, print, write nothing
    python publications_tracker.py --days 14
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "publications" / "data"
USER_AGENT = "MothsForAnna-Publications/1.0 (https://mothsforanna.com)"

GROUPS = {
    "moth": {
        "pubmed": '(moth[Title/Abstract] OR moths[Title/Abstract] OR Heterocera[Title/Abstract])',
        "terms": [r"moths?", r"heterocera"],
        "scholar": ['moth ecology OR behavior OR evolution -insecticide -pest -crop'],
    },
    "nudibranch": {
        "pubmed": '(nudibranch[Title/Abstract] OR nudibranchs[Title/Abstract] OR Nudibranchia[Title/Abstract])',
        "terms": [r"nudibranchs?", r"nudibranchia"],
        "scholar": ['nudibranch OR nudibranchs OR Nudibranchia'],
    },
    "amphibian": {
        "pubmed": ('(frog[Title] OR frogs[Title] OR toad[Title] OR toads[Title] OR '
                   'salamander[Title] OR salamanders[Title] OR newt[Title] OR newts[Title] OR '
                   'caecilian[Title] OR caecilians[Title] OR axolotl[Title] OR axolotls[Title] OR '
                   'tadpole[Title] OR tadpoles[Title] OR amphibian[Title] OR amphibians[Title])'),
        "terms": [r"frogs?", r"toads?", r"salamanders?", r"newts?", r"caecilians?",
                  r"axolotls?", r"tadpoles?", r"amphibians?"],
        "title_only": True,
        "scholar": ['frog OR salamander OR amphibian ecology OR behavior OR conservation'],
    },
}

PEST_TERMS = [
    r"insecticid\w*", r"pesticid\w*", r"pests?", r"crops?", r"larvicid\w*",
    r"biocontrol", r"biological control", r"mating disruption", r"integrated pest management",
    r"yield loss\w*", r"bacillus thuringiensis", r"bt (?:cotton|maize|corn|toxin|protein)s?",
    r"cry1\w*", r"armyworms?", r"bollworms?", r"cutworms?", r"stem borers?", r"fruit borers?",
    r"spodoptera", r"helicoverpa", r"plutella", r"diamondback moth", r"codling moth",
    r"ostrinia", r"tuta absoluta", r"phthorimaea", r"grapholita", r"lymantria",
]
PEST_RE = re.compile(r"\b(?:" + "|".join(PEST_TERMS) + r")\b", re.I)


def norm_title(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def paper_key(p):
    if p.get("doi"):
        return "doi:" + p["doi"].lower()
    return "t:" + hashlib.md5(norm_title(p["title"]).encode()).hexdigest()


def get(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ! request failed: {url[:90]}… ({e})")
                return None
            time.sleep(2 * (attempt + 1))


def text_of(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def is_pest(p):
    return bool(PEST_RE.search(p["title"] + " " + p.get("abstract", "")))


def matches_group(group, title, abstract):
    cfg = GROUPS[group]
    hay = title if cfg.get("title_only") else f"{title} {abstract}"
    return any(re.search(rf"\b{t}\b", hay, re.I) for t in cfg["terms"])


# ── PubMed ──────────────────────────────────────────────────

def search_pubmed(group, days):
    key = os.environ.get("NCBI_API_KEY", "")
    kp = f"&api_key={key}" if key else ""
    today = datetime.date.today()
    mind = (today - datetime.timedelta(days=days)).strftime("%Y/%m/%d")
    maxd = today.strftime("%Y/%m/%d")
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed"
           f"&term={urllib.parse.quote(GROUPS[group]['pubmed'])}"
           f"&mindate={mind}&maxdate={maxd}&datetype=edat&retmax=500&retmode=json{kp}")
    data = get(url)
    if data is None:
        return None
    ids = json.loads(data).get("esearchresult", {}).get("idlist", [])
    papers = []
    for i in range(0, len(ids), 100):
        time.sleep(0.4)
        xml = get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed"
                  f"&id={','.join(ids[i:i + 100])}&retmode=xml{kp}")
        if xml is None:
            return None
        for art in ET.fromstring(xml).findall(".//PubmedArticle"):
            p = parse_pubmed(art)
            if p:
                papers.append(p)
    return papers


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_pubmed(art):
    try:
        med = art.find(".//MedlineCitation")
        pmid = med.findtext("PMID", "")
        a = med.find("Article")
        title = text_of(a.find("ArticleTitle"))
        if not title:
            return None
        authors = []
        for au in a.findall(".//AuthorList/Author"):
            last, fore = au.findtext("LastName", ""), au.findtext("ForeName", "")
            coll = au.findtext("CollectiveName", "")
            if last:
                authors.append(f"{fore} {last}".strip())
            elif coll:
                authors.append(coll)
        parts = []
        for ab in a.findall(".//Abstract/AbstractText"):
            label, txt = ab.get("Label", ""), text_of(ab)
            parts.append(f"{label}: {txt}" if label else txt)
        pd = a.find(".//JournalIssue/PubDate")
        y = pd.findtext("Year", "") if pd is not None else ""
        if not y and pd is not None:
            y = (pd.findtext("MedlineDate", "") or "")[:4]
        mo = (pd.findtext("Month", "") if pd is not None else "")[:3].lower()
        mo = MONTHS.get(mo) or (int(mo) if mo.isdigit() else None)
        date = f"{y}-{mo:02d}" if y and mo else y
        doi = next((e.text for e in art.findall(".//PubmedData/ArticleIdList/ArticleId")
                    if e.get("IdType") == "doi" and e.text), "")
        return {
            "source": "PubMed", "pmid": pmid, "title": title, "authors": authors,
            "abstract": " ".join(parts), "journal": a.findtext("Journal/Title", ""),
            "date": date, "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
    except Exception:
        return None


# ── bioRxiv ─────────────────────────────────────────────────

def fetch_biorxiv(days):
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days)).isoformat()
    out, cursor = [], 0
    while cursor < 20000:
        data = get(f"https://api.biorxiv.org/details/biorxiv/{start}/{today.isoformat()}/{cursor}")
        if data is None:
            return None
        coll = json.loads(data).get("collection", [])
        out.extend(coll)
        if len(coll) < 100:
            break
        cursor += 100
        time.sleep(0.3)
    return out


def biorxiv_for(group, raw):
    papers, seen = [], set()
    for r in raw:
        doi, title, abstract = r.get("doi", ""), r.get("title", ""), r.get("abstract", "")
        if doi in seen or not matches_group(group, title, abstract):
            continue
        seen.add(doi)
        papers.append({
            "source": "bioRxiv", "title": re.sub(r"\s+", " ", title).strip(),
            "authors": [a.strip() for a in r.get("authors", "").split(";") if a.strip()],
            "abstract": re.sub(r"\s+", " ", abstract).strip(), "journal": "bioRxiv (preprint)",
            "date": r.get("date", "")[:7], "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else "",
        })
    return papers


# ── Google Scholar (SerpAPI) ───────────────────────────────

def search_scholar(group):
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        print("  Google Scholar: SERPAPI_KEY not set, skipping.")
        return []
    year = datetime.date.today().year
    papers = []
    for q in GROUPS[group]["scholar"]:
        params = urllib.parse.urlencode({"engine": "google_scholar", "q": q, "as_ylo": year,
                                         "scisbd": 1, "num": 20, "api_key": key})
        data = get(f"https://serpapi.com/search.json?{params}")
        if data is None:
            continue
        for it in json.loads(data).get("organic_results", []):
            title = (it.get("title") or "").strip()
            snippet = it.get("snippet", "")
            if not title or not matches_group(group, title, snippet):
                continue
            summary = it.get("publication_info", {}).get("summary", "")
            authors = [a.strip() for a in summary.split(" - ")[0].split(",") if a.strip()]
            venue = summary.split(" - ")[1].rsplit(",", 1)[0].strip() if summary.count(" - ") >= 1 else ""
            papers.append({
                "source": "Google Scholar", "title": title, "authors": authors,
                "abstract": snippet, "journal": venue, "date": str(year), "doi": "",
                "url": it.get("link", ""),
            })
        time.sleep(1)
    return papers


# ── Storage ────────────────────────────────────────────────

def load_existing():
    store = {}
    if not DATA_DIR.exists():
        return store
    for f in sorted(DATA_DIR.glob("[0-9][0-9][0-9][0-9].json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            assert isinstance(doc.get("papers"), list)
        except Exception as e:
            sys.exit(f"ABORT: {f} could not be read ({e}). Nothing was written.")
        store[f.stem] = doc["papers"]
    return store


def atomic_write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    json.loads(Path(tmp).read_text(encoding="utf-8"))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = load_existing()
    known = {paper_key(p) for papers in store.values() for p in papers}
    known |= {"t:" + hashlib.md5(norm_title(p["title"]).encode()).hexdigest()
              for papers in store.values() for p in papers}
    before = sum(len(v) for v in store.values())

    print("bioRxiv: fetching recent preprints…")
    raw_bx = fetch_biorxiv(args.days)

    found, by_title, failures = {}, {}, []
    for group in GROUPS:
        print(f"[{group}]")
        pm = search_pubmed(group, args.days)
        if pm is None:
            failures.append(f"{group}/PubMed")
        bx = biorxiv_for(group, raw_bx) if raw_bx is not None else None
        if bx is None:
            failures.append(f"{group}/bioRxiv")
        gs = search_scholar(group)
        candidates = [p for p in (pm or []) if matches_group(group, p["title"], p["abstract"])]
        candidates += (bx or []) + gs
        kept = pest = 0
        for p in candidates:
            if is_pest(p):
                pest += 1
                continue
            k = paper_key(p)
            tk = "t:" + hashlib.md5(norm_title(p["title"]).encode()).hexdigest()
            if k in known or tk in known:
                continue
            existing = found.get(k) or found.get(by_title.get(tk))
            if existing:
                if group not in existing["groups"]:
                    existing["groups"].append(group)
                continue
            p["groups"] = [group]
            found[k] = p
            by_title[tk] = k
            kept += 1
        print(f"  PubMed {len(pm or [])}, bioRxiv {len(bx or [])}, Scholar {len(gs)} "
              f"→ {pest} pest papers dropped, {kept} new")

    today = datetime.date.today().isoformat()
    new = sorted(found.values(), key=lambda p: p["title"].lower())
    for p in new:
        p["added"] = today
        p["id"] = paper_key(p)

    print(f"\n{len(new)} new papers; {before} already stored.")
    if failures:
        print("Sources that failed this run: " + ", ".join(failures))
    if args.dry_run:
        for p in new:
            print(f"  [{'/'.join(p['groups'])}] {p['source']}: {p['title'][:100]}")
        return

    year = today[:4]
    papers_year = store.get(year, [])
    updated = new + papers_year
    assert len(updated) == len(papers_year) + len(new)
    if new:
        atomic_write(DATA_DIR / f"{year}.json", {"year": int(year), "papers": updated})
    years = sorted({*store.keys(), year} if new else set(store.keys()), reverse=True)
    atomic_write(DATA_DIR / "index.json", {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "years": [int(y) for y in years],
        "total": before + len(new),
        "last_run_new": len(new),
        "last_run_failures": failures,
    })
    print(f"Wrote {len(new)} papers to {year}.json; total now {before + len(new)}.")
    if len(failures) == 3 * 2:
        sys.exit("Every PubMed and bioRxiv request failed.")


if __name__ == "__main__":
    main()
