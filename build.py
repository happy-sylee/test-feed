#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
논문 피드 빌더 (v2: 그룹 사이드바 + 날짜 그룹핑 + 라이트모드)
- feeds.txt (그룹|저널|URL) 의 RSS 를 모두 읽어서
- 최근 N일 + 키워드 매칭 논문만 골라
- 데이터를 JSON 으로 index.html 에 심는다.
- 사이드바 필터/정렬은 브라우저(JS)가 처리 (정적 호스팅에서도 동작)
"""

import html
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import urllib.request
import urllib.error

# ------------------- 설정 -------------------
DAYS_BACK = 14
HERE = Path(__file__).parent
FEEDS_FILE = HERE / "feeds.txt"
KEYWORDS_FILE = HERE / "keywords.txt"
OUTPUT_FILE = HERE / "index.html"
KST = timezone(timedelta(hours=9))
# --------------------------------------------


def load_feeds():
    """feeds.txt -> [(group, name, method, value), ...]
    세 번째 칸은 'rss:주소' 또는 'crossref:ISSN' 형식"""
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            continue
        group, name, spec = parts
        # spec = "rss:..." 또는 "crossref:..."
        if ":" not in spec:
            continue
        method, value = spec.split(":", 1)
        method = method.strip().lower()
        value = value.strip()
        feeds.append((group, name, method, value))
    return feeds


def load_categories():
    """keywords.txt 를 파싱.
    반환: (cats, order, battery_kws)
      cats = {"SYSTEM": {...}, "COMPONENT": {...}}  (태깅용)
      order = 축별 카테고리 순서
      battery_kws = [BATTERY] 섹션 키워드 리스트 (수집 필터용)"""
    cats = {"SYSTEM": {}, "COMPONENT": {}}
    order = {"SYSTEM": [], "COMPONENT": []}
    battery_kws = []
    cur_axis = None
    cur_cat = None
    in_battery = False
    for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            inner = line[1:-1]
            if inner.strip().upper() == "BATTERY":
                in_battery = True
                cur_axis = cur_cat = None
                continue
            in_battery = False
            if ":" in inner:
                axis, cat = inner.split(":", 1)
                axis = axis.strip().upper()
                cat = cat.strip()
                if axis in cats:
                    cur_axis, cur_cat = axis, cat
                    if cat not in cats[axis]:
                        cats[axis][cat] = []
                        order[axis].append(cat)
                else:
                    cur_axis = cur_cat = None
            continue
        # 키워드 줄
        if in_battery:
            battery_kws.append(line.lower())
        elif cur_axis and cur_cat:
            cats[cur_axis][cur_cat].append(line.lower())
    return cats, order, battery_kws


def filter_keywords(cats, battery_kws):
    """수집 필터용 키워드 집합: BATTERY 섹션 + SYSTEM 축 키워드만.
    (COMPONENT 단독으로는 통과 못 하게 → 배터리 무관 논문 차단)"""
    kws = set(battery_kws)
    for kwlist in cats["SYSTEM"].values():
        kws.update(kwlist)
    return kws


def tag_categories(text, cats):
    """text가 속하는 카테고리들을 축별로 반환.
    반환: {"systems": [...], "components": [...]}
    아무 카테고리에도 안 걸리면 ['others']"""
    low = text.lower()
    systems = []
    for cat, kwlist in cats["SYSTEM"].items():
        if any(kw in low for kw in kwlist):
            systems.append(cat)
    components = []
    for cat, kwlist in cats["COMPONENT"].items():
        if any(kw in low for kw in kwlist):
            components.append(cat)
    if not systems:
        systems = ["others"]
    if not components:
        components = ["others"]
    return {"systems": systems, "components": components}


def clean_html(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_authors(entry, max_authors=4):
    """RSS entry에서 저자 목록을 추출. 형식이 제각각이라 방어적으로 처리.
    - entry.authors (리스트) 또는 entry.author (문자열) 시도
    - 너무 많으면 max_authors까지 + 'et al.'
    - 없으면 빈 문자열"""
    names = []
    if entry.get("authors"):
        for a in entry["authors"]:
            name = a.get("name", "").strip() if isinstance(a, dict) else str(a).strip()
            if name:
                names.append(name)
    if not names and entry.get("author"):
        raw = str(entry["author"]).strip()
        if raw:
            parts = re.split(r"\s*(?:,|;|\band\b|&)\s*", raw)
            names = [p.strip() for p in parts if p.strip()]
    names = [clean_html(n) for n in names if n]
    if not names:
        return ""
    if len(names) > max_authors:
        return ", ".join(names[:max_authors]) + " et al."
    return ", ".join(names)


def get_entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def matches_any(text, keyword_set):
    """text 안에 키워드 집합 중 하나라도 있으면 True (수집 필터용)"""
    low = text.lower()
    return any(kw in low for kw in keyword_set)


def fetch_feed(url, timeout=30):
    """RSS를 브라우저인 척 헤더를 붙여서 받아온 뒤 feedparser로 파싱.
    출판사 서버가 자동 접속(봇)을 차단하는 걸 우회하기 위함.
    반환: (parsed_feed, 상태문자열)"""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
        "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        parsed = feedparser.parse(raw)
        return parsed, f"HTTP {resp.status if hasattr(resp,'status') else 200}, {len(raw)}바이트"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} 에러"
    except Exception as e:
        # urllib 실패 시 feedparser 기본 방식으로 한 번 더 시도
        try:
            parsed = feedparser.parse(url)
            if parsed.entries:
                return parsed, "기본방식 성공"
        except Exception:
            pass
        return None, f"실패: {type(e).__name__}"


def fetch_crossref(issn, days_back, timeout=30):
    """Crossref API로 특정 ISSN 저널의 최근 논문을 받아온다.
    RSS가 봇 차단되는 출판사(Wiley, ACS 등) 대응용.
    반환: (entries 리스트, 상태문자열)
    각 entry는 feedparser와 유사하게 title/summary/link/authors/date를 흉내낸 dict."""
    import json
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    # from-index-date: 최근 색인된 것 (신간 잡기 좋음). rows 넉넉히.
    url = (f"https://api.crossref.org/journals/{issn}/works"
           f"?filter=from-index-date:{since}"
           f"&sort=published&order=desc&rows=60"
           f"&mailto=paper-feed@example.com")  # 매너용 이메일 (빠른 응답)
    headers = {"User-Agent": "PaperFeed/1.0 (mailto:paper-feed@example.com)"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw)
        items = data.get("message", {}).get("items", [])
        entries = []
        for it in items:
            # 제목
            title_list = it.get("title", [])
            title = title_list[0] if title_list else ""
            if not title:
                continue  # 제목 없으면 스킵
            # 초록 (있으면. Crossref는 JATS XML 형태로 줌)
            abstract = it.get("abstract", "")
            # 저자
            authors = []
            for a in it.get("author", []):
                nm = " ".join(filter(None, [a.get("given",""), a.get("family","")])).strip()
                if nm:
                    authors.append({"name": nm})
            # 링크 (DOI URL)
            link = it.get("URL", "") or (f"https://doi.org/{it.get('DOI','')}" if it.get("DOI") else "")
            # 날짜: published 우선, 없으면 created/indexed
            date_parts = None
            for key in ("published", "published-online", "published-print", "created", "indexed"):
                dp = it.get(key, {}).get("date-parts", [[]])
                if dp and dp[0] and dp[0][0]:
                    date_parts = dp[0]
                    break
            published_parsed = None
            if date_parts:
                y = date_parts[0]
                m = date_parts[1] if len(date_parts) > 1 else 1
                d = date_parts[2] if len(date_parts) > 2 else 1
                try:
                    import time as _t
                    dt = datetime(y, m, d, tzinfo=timezone.utc)
                    published_parsed = dt.timetuple()
                except Exception:
                    published_parsed = None
            entries.append({
                "title": title,
                "summary": abstract,
                "link": link,
                "authors": authors,
                "published_parsed": published_parsed,
            })
        return entries, f"Crossref {len(entries)}건 ({len(raw)}바이트)"
    except urllib.error.HTTPError as e:
        return None, f"Crossref HTTP {e.code}"
    except Exception as e:
        return None, f"Crossref 실패: {type(e).__name__}"


def main():
    feeds = load_feeds()
    cats, cat_order, battery_kws = load_categories()
    keyword_set = filter_keywords(cats, battery_kws)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DAYS_BACK)

    items = []
    groups_order = []  # 그룹 등장 순서 보존
    log = []

    for group, name, method, value in feeds:
        if group not in groups_order:
            groups_order.append(group)
        try:
            # 방식에 따라 entries 리스트 확보
            if method == "crossref":
                entries, status = fetch_crossref(value, DAYS_BACK)
                if entries is None:
                    log.append(f"  [실패] {group} / {name}: {status}")
                    continue
            else:  # rss
                parsed, status = fetch_feed(value)
                if parsed is None:
                    log.append(f"  [실패] {group} / {name}: {status}")
                    continue
                entries = parsed.entries

            n_total = len(entries)
            n_kept = 0
            for entry in entries:
                title = clean_html(entry.get("title", ""))
                abstract = clean_html(entry.get("summary", ""))
                if not abstract and entry.get("content"):
                    try:
                        abstract = clean_html(entry["content"][0].get("value", ""))
                    except Exception:
                        abstract = ""
                link = entry.get("link", "")
                date = get_entry_date(entry)
                # 미래 날짜 보정: Crossref가 예정 출판일(미래)을 주는 경우 오늘로 클램프
                if date and date > now:
                    date = now
                authors = get_authors(entry)

                if date and date < cutoff:
                    continue
                haystack = title + " " + abstract
                if not matches_any(haystack, keyword_set):
                    continue

                # 카테고리 태깅 (시스템 축 + 구성요소 축)
                tags = tag_categories(haystack, cats)

                # 날짜를 KST 기준 yyyy-mm-dd 문자열로 (없으면 빈 문자열)
                date_kst = date.astimezone(KST).strftime("%Y-%m-%d") if date else ""
                # 정렬용 타임스탬프 (없으면 0)
                ts = date.timestamp() if date else 0

                items.append({
                    "group": group,
                    "journal": name,
                    "title": title or "(제목 없음)",
                    "abstract": abstract,
                    "link": link,
                    "date": date_kst,
                    "ts": ts,
                    "authors": authors,
                    "systems": tags["systems"],
                    "components": tags["components"],
                })
                n_kept += 1
            log.append(f"  [OK]  {group} / {name}: {n_total}개 중 {n_kept}개  ({status})")
        except Exception as e:
            log.append(f"  [실패] {group} / {name}: {e}")

    now_kst_str = now.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    html_out = render_html(items, groups_order, cat_order, now_kst_str)
    OUTPUT_FILE.write_text(html_out, encoding="utf-8")

    print(f"총 {len(items)}개 논문 수집 (최근 {DAYS_BACK}일)")
    print("\n".join(log))


def render_html(items, groups_order, cat_order, now_str):
    # 데이터를 JS로 안전하게 전달 (</script> 깨짐 방지)
    data_json = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    groups_json = json.dumps(groups_order, ensure_ascii=False)
    systems_json = json.dumps(cat_order["SYSTEM"] + ["others"], ensure_ascii=False)
    components_json = json.dumps(cat_order["COMPONENT"] + ["others"], ensure_ascii=False)

    return """<!DOCTYPE html>
<html lang="ko" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>논문 피드</title>
<style>
  :root[data-theme="dark"] {
    --bg:#0f1115; --sidebar:#14171d; --card:#1a1d24; --border:#2a2e38;
    --text:#e8eaed; --muted:#9aa0a6; --accent:#6ea8fe; --date-bar:#222732;
    /* 그룹별 색 (다크: 밝은 글자색) */
    --g-Nature:#f06a35; --g-Science:#f08080; --g-Wiley:#6ea8fe;
    --g-RSC:#5fc98a; --g-Elsevier:#e6c34a; --g-ACS:#8a93e0;
    /* 시스템 카테고리 색 (다크) */
    --sys-LIB:#5ad1c8; --sys-LMB:#b48ef0; --sys-SIB:#5fc98a;
    --sys-ZIB:#e88fb8; --sys-others:#d9a441;
  }
  :root[data-theme="light"] {
    --bg:#f6f7f9; --sidebar:#ffffff; --card:#ffffff; --border:#e2e5ea;
    --text:#1a1d24; --muted:#6b7280; --accent:#2563eb; --date-bar:#eef1f6;
    /* 그룹별 색 (라이트: 진한 글자색) */
    --g-Nature:#ea5c27; --g-Science:#c43d3d; --g-Wiley:#2563eb;
    --g-RSC:#1f9254; --g-Elsevier:#a37e12; --g-ACS:#4750b0;
    /* 시스템 카테고리 색 (라이트) */
    --sys-LIB:#0e9b90; --sys-LMB:#7c3aed; --sys-SIB:#1f9254;
    --sys-ZIB:#c43d7a; --sys-others:#b8740f;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo","Noto Sans KR",sans-serif;
    line-height:1.6;
  }
  .layout { display:flex; min-height:100vh; }

  /* ---- 사이드바 ---- */
  .sidebar {
    width:180px; flex-shrink:0; background:var(--sidebar);
    border-right:1px solid var(--border); padding:16px 10px;
    position:sticky; top:0; height:100vh; overflow-y:auto;
  }
  .sidebar h2 { font-size:13px; color:var(--muted); margin:6px 8px 10px; font-weight:600; letter-spacing:.04em; }
  .tab {
    display:flex; justify-content:space-between; align-items:center;
    padding:8px 10px; margin-bottom:2px; border-radius:8px;
    cursor:pointer; font-size:14px; color:var(--text); user-select:none;
  }
  .tab:hover { background:var(--border); }
  .tab.active { background:var(--accent); color:#fff; }
  .tab .count { font-size:11px; opacity:.7; }
  .tab.active .count { opacity:.9; }
  .folder-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .folder-io { display:flex; gap:6px; margin-top:12px; padding:0 4px; }
  .folder-io button {
    flex:1; font-size:11px; color:var(--muted);
    background:var(--card); border:1px solid var(--border);
    border-radius:6px; padding:6px 4px; cursor:pointer;
  }
  .folder-io button:hover { border-color:var(--accent); color:var(--accent); }
  .hidden-info {
    margin-top:14px; padding:8px 10px; font-size:11px; color:var(--muted);
    display:flex; flex-direction:column; gap:6px; align-items:flex-start;
    border-top:1px solid var(--border);
  }
  .hidden-info[hidden] { display:none; }
  #restoreHidden {
    font-size:11px; color:var(--muted); background:none;
    border:1px solid var(--border); border-radius:6px;
    padding:4px 10px; cursor:pointer;
  }
  #restoreHidden:hover { border-color:var(--accent); color:var(--accent); }

  /* ---- 메인 ---- */
  .main { flex:1; min-width:0; }
  .topbar {
    position:sticky; top:0; z-index:10;
    background:var(--bg); border-bottom:1px solid var(--border);
  }
  header {
    background:var(--bg);
    padding:14px 24px;
    display:flex; justify-content:space-between; align-items:center; gap:12px;
  }
  header h1 { margin:0; font-size:18px; }
  header .meta { color:var(--muted); font-size:12px; margin-top:2px; }
  .theme-btn {
    background:var(--card); border:1px solid var(--border); color:var(--text);
    border-radius:8px; padding:7px 12px; cursor:pointer; font-size:13px; white-space:nowrap;
  }
  .theme-btn:hover { border-color:var(--accent); }

  .content { max-width:860px; margin:0 auto; padding:20px 24px 60px; }

  /* ---- 날짜 헤더 ---- */
  .date-header {
    background:var(--date-bar); border:1px solid var(--border);
    border-radius:8px; padding:6px 14px; margin:22px 0 12px;
    font-size:13px; font-weight:600; color:var(--accent);
  }
  .date-header:first-child { margin-top:0; }

  /* ---- 카드 ---- */
  .card {
    background:var(--card); border:1px solid var(--border);
    border-radius:12px; padding:15px 18px; margin-bottom:12px;
  }
  .card:hover { border-color:var(--accent); }
  .card.read { opacity:0.55; }
  .card.read:hover { opacity:0.8; }
  .card-head { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:6px; }
  .journal { font-size:11.5px; font-weight:600;
    color:var(--jcolor, var(--accent));
    background:color-mix(in srgb, var(--jcolor, var(--accent)) 14%, transparent);
    padding:2px 9px; border-radius:999px; }
  .grp { font-size:11px; color:var(--muted); }
  .save-btn {
    background:none; border:none; cursor:pointer; font-size:16px;
    color:var(--muted); padding:0 2px; line-height:1; transition:color .12s, transform .12s;
  }
  .save-btn:hover { color:var(--accent); transform:scale(1.2); }
  .claude-btn {
    background:none; border:1px solid var(--border); cursor:pointer;
    font-size:11px; font-weight:700; color:var(--muted);
    width:20px; height:20px; border-radius:5px; line-height:1;
    transition:all .12s;
  }
  .claude-btn:hover { color:#fff; background:var(--accent); border-color:var(--accent); }
  @media (max-width:680px) { .claude-btn { display:none; } }
  .hide-btn {
    background:none; border:none; cursor:pointer; font-size:14px;
    padding:0 2px; line-height:1; opacity:0.5; transition:opacity .12s, transform .12s;
  }
  .hide-btn:hover { opacity:1; transform:scale(1.15); }
  .title { font-size:16px; margin:2px 0 6px; line-height:1.4; }
  .title a { color:var(--text); text-decoration:none; }
  .title a:hover { color:var(--accent); text-decoration:underline; }
  .authors { font-size:12.5px; color:var(--muted); margin:0 0 8px; }
  .abstract { margin:0; color:var(--muted); font-size:14px; }
  .no-abstract { font-style:italic; opacity:.6; }
  /* 카드 안 카테고리 태그 */
  .cat-tags { margin-top:9px; display:flex; flex-wrap:wrap; gap:5px; }
  .cat-tag {
    font-size:11px; font-weight:500;
    padding:1px 9px; border-radius:6px; border:1px solid transparent;
  }
  .cat-tag.system {
    color:var(--sys-color, var(--muted));
    background:color-mix(in srgb, var(--sys-color, var(--muted)) 12%, transparent);
    border-color:color-mix(in srgb, var(--sys-color, var(--muted)) 30%, transparent);
  }
  .cat-tag.component {
    color:var(--muted);
    background:color-mix(in srgb, var(--text) 7%, transparent);
    border-color:var(--border);
  }

  /* 상단 필터 바 (토글로 열고 닫음) */
  .filterbar {
    background:var(--sidebar);
    padding:14px 24px; display:flex; flex-direction:column; gap:10px;
    border-top:1px solid var(--border);
  }
  .filterbar[hidden] { display:none; }
  .filter-actions { display:flex; gap:8px; margin-top:2px; }
  /* 검색 바 */
  .searchbar {
    background:var(--sidebar); border-top:1px solid var(--border);
    padding:12px 24px; display:flex; gap:8px; align-items:center;
  }
  .searchbar[hidden] { display:none; }
  .search-icon { font-size:15px; opacity:.7; }
  #searchInput {
    flex:1; background:var(--bg); border:1px solid var(--border);
    border-radius:8px; padding:9px 13px; color:var(--text); font-size:14px;
  }
  #searchInput:focus { outline:none; border-color:var(--accent); }
  .search-clear {
    background:none; border:1px solid var(--border); color:var(--muted);
    border-radius:8px; padding:8px 12px; cursor:pointer; font-size:13px;
  }
  .search-clear:hover { border-color:var(--accent); color:var(--accent); }
  .filter-clear {
    font-size:12px; color:var(--muted); background:none; border:none;
    cursor:pointer; text-decoration:underline; padding:0;
  }
  .filter-clear:hover { color:var(--accent); }
  .filter-row { display:flex; align-items:center; gap:10px; }
  .filter-label {
    font-size:11px; font-weight:600; color:var(--muted);
    min-width:52px; letter-spacing:.03em;
  }
  .chips { display:flex; flex-wrap:wrap; gap:6px; }
  .chip {
    font-size:12px; padding:3px 11px; border-radius:999px;
    border:1px solid var(--border); background:var(--card); color:var(--muted);
    cursor:pointer; user-select:none; transition:all .12s;
  }
  .chip:hover { border-color:var(--accent); color:var(--text); }
  .chip.on {
    background:var(--accent); color:#fff; border-color:var(--accent);
  }
  /* 날짜 선택기 */
  .date-picker { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .date-input {
    background:var(--card); border:1px solid var(--border); color:var(--text);
    border-radius:8px; padding:5px 10px; font-size:13px; cursor:pointer;
    font-family:inherit;
  }
  .date-input:focus { outline:none; border-color:var(--accent); }
  .date-selected {
    font-size:12px; color:var(--accent); font-weight:600;
    background:color-mix(in srgb, var(--accent) 12%, transparent);
    padding:3px 10px; border-radius:999px; cursor:pointer;
  }
  .date-selected[hidden] { display:none; }
  .empty { text-align:center; color:var(--muted); padding:60px 0; }
  /* 맨 위로 버튼 */
  #scrollTop {
    position:fixed; right:24px; bottom:24px; z-index:30;
    width:44px; height:44px; border-radius:50%;
    background:var(--accent); color:#fff; border:none;
    font-size:20px; cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.25);
    opacity:0; pointer-events:none; transition:opacity .2s;
  }
  #scrollTop.show { opacity:0.9; pointer-events:auto; }
  #scrollTop:hover { opacity:1; }

  /* ---- 모바일: 사이드바를 가운데 드롭다운으로 ---- */
  .dropdown-toggle { display:none; }
  @media (max-width:680px) {
    .layout { flex-direction:column; }
    /* 사이드바를 상단 고정 컨트롤 영역으로 */
    .sidebar {
      width:100%; height:auto; position:sticky; top:0; z-index:20;
      border-right:none; border-bottom:1px solid var(--border);
      padding:8px 12px; display:flex; flex-direction:column; align-items:stretch;
    }
    .sidebar h2 { display:none; }
    .folder-io { display:none; }              /* 모바일선 내보내기/가져오기 숨김 (공간 절약) */
    #folderList .tab + * { }
    /* 안내문구 숨김 */
    #folderList > div[style*="hint"], .folder-hint { display:none; }
    /* 상단 버튼 줄: 메뉴(저널/폴더) + 검색 + 필터 */
    .mobile-bar { display:flex; gap:6px; }
    .dropdown-toggle {
      display:flex; justify-content:space-between; align-items:center; gap:6px;
      flex:1;
      background:var(--card); border:1px solid var(--border); color:var(--text);
      border-radius:10px; padding:9px 12px; cursor:pointer; font-size:13px; font-weight:600;
    }
    .dropdown-toggle .arrow { transition:transform .2s; color:var(--muted); }
    .sidebar.open .dropdown-toggle .arrow { transform:rotate(180deg); }
    /* 펼쳐지는 영역: 저널그룹 + 폴더 */
    .sidebar-content {
      display:none; flex-direction:column; gap:2px;
      margin-top:6px; max-height:60vh; overflow-y:auto;
    }
    .sidebar.open .sidebar-content { display:flex; }
    #tabs { display:flex; flex-direction:column; gap:2px; }
    .tab { justify-content:space-between; }
    /* topbar(제목/메타) 일반 흐름, 검색·필터 버튼은 모바일바로 이동 */
    .topbar { position:static; }
    header { padding:10px 14px; }
    header h1 { font-size:16px; }
    /* 데스크탑 헤더의 버튼들은 모바일에서 숨기고, 모바일바 버튼 사용 */
    .desktop-btns { display:none; }
    .mobile-only-btn {
      display:flex; align-items:center; justify-content:center;
      background:var(--card); border:1px solid var(--border); color:var(--text);
      border-radius:10px; padding:9px 11px; cursor:pointer; font-size:13px; white-space:nowrap;
    }
    #scrollTop { right:16px; bottom:16px; width:40px; height:40px; }
  }
  /* 데스크탑에선 모바일 전용 요소 숨김 */
  .mobile-bar { display:none; }
  .mobile-only-btn { display:none; }
</style>
</head>
<body>
<div class="layout">
  <nav class="sidebar" id="sidebar">
    <div class="mobile-bar">
      <button class="dropdown-toggle" id="dropdownToggle">
        <span id="currentGroup">전체</span>
        <span class="arrow">▾</span>
      </button>
      <button class="mobile-only-btn" id="searchToggleM">🔍</button>
      <button class="mobile-only-btn" id="filterToggleM">🔬</button>
      <button class="mobile-only-btn" id="themeBtnM">🌙</button>
    </div>
    <div class="sidebar-content">
      <h2>출판사 그룹</h2>
      <div id="tabs"></div>
      <h2 style="margin-top:18px; display:flex; justify-content:space-between; align-items:center;">
        <span>내 폴더</span>
        <span id="addFolder" style="cursor:pointer; color:var(--accent); font-size:16px;">＋</span>
      </h2>
      <div id="folderList"></div>
      <div class="folder-io">
        <button id="exportFolders" title="폴더를 파일로 내보내기">내보내기</button>
        <button id="importFolders" title="파일에서 폴더 가져오기">가져오기</button>
        <input type="file" id="importFile" accept=".json" hidden>
      </div>
      <div id="hiddenInfo" class="hidden-info" hidden>
        <span id="hiddenCount"></span>
        <button id="restoreHidden">되살리기</button>
      </div>
    </div>
  </nav>
  <div class="main">
    <div class="topbar">
    <header>
      <div>
        <h1>📚 논문 피드</h1>
        <div class="meta">최근 14일 · battery 관련 · 업데이트 __NOW__ KST</div>
      </div>
      <div class="desktop-btns" style="display:flex; gap:8px; align-items:center;">
        <button class="theme-btn" id="searchToggle">🔍 검색</button>
        <button class="theme-btn" id="filterToggle">🔬 필터</button>
        <button class="theme-btn" id="themeBtn">🌙 다크</button>
      </div>
    </header>
    <div class="filterbar" id="filterbar" hidden>
      <div class="filter-row">
        <span class="filter-label">날짜</span>
        <div class="date-picker">
          <span class="chip" id="dateAll">전체</span>
          <input type="date" id="datePicker" class="date-input">
          <span class="date-selected" id="dateSelected" hidden></span>
        </div>
      </div>
      <div class="filter-row">
        <span class="filter-label">시스템</span>
        <div class="chips" id="systemChips"></div>
      </div>
      <div class="filter-row">
        <span class="filter-label">구성요소</span>
        <div class="chips" id="componentChips"></div>
      </div>
      <div class="filter-actions">
        <button class="filter-clear" id="filterClear">필터 초기화</button>
      </div>
    </div>
    <div class="searchbar" id="searchbar" hidden>
      <span class="search-icon">🔍</span>
      <input type="text" id="searchInput" placeholder="제목 · 초록 · 저자에서 검색...">
      <button class="search-clear" id="searchClear" title="검색 닫기">✕</button>
    </div>
    </div>
    <div class="content" id="content"></div>
  </div>
</div>
<button id="scrollTop" title="맨 위로">↑</button>

<script>
const ITEMS = __DATA__;
const GROUPS = __GROUPS__;
const SYSTEMS = __SYSTEMS__;
const COMPONENTS = __COMPONENTS__;
let activeGroup = "전체";
let activeSystems = new Set();      // 선택된 시스템 칩 (비어있으면 전체)
let activeComponents = new Set();   // 선택된 구성요소 칩
let searchQuery = "";               // 검색어 (제목/초록/저자)
let activeDate = "";                 // 선택된 날짜 (빈 문자열=전체)

// ---- 폴더 (localStorage 기반) ----
// 구조: { "폴더명": [ {논문 통째 복사}, ... ], ... }
const STORE_KEY = "paperFeedFolders";
let folders = loadFolders();
let viewMode = "feed";   // "feed" = 메인 피드, "folder" = 특정 폴더 보기
let activeFolder = null;
let currentList = [];    // 현재 화면에 그려진 논문 목록 (저장 버튼 참조용)

function loadFolders(){
  try {
    const raw = localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch(e){ return {}; }
}
function saveFolders(){
  try { localStorage.setItem(STORE_KEY, JSON.stringify(folders)); }
  catch(e){ alert("저장 공간이 부족하거나 브라우저가 저장을 막고 있어요."); }
}
function paperKey(it){
  // 중복 판단용 키 (링크 우선, 없으면 제목)
  return it.link || it.title;
}
function isSaved(folderName, it){
  return (folders[folderName]||[]).some(p => paperKey(p) === paperKey(it));
}

// ---- 읽음 표시 (localStorage) ----
const READ_KEY = "paperFeedRead";
let readSet = loadReadSet();
function loadReadSet(){
  try {
    const raw = localStorage.getItem(READ_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch(e){ return new Set(); }
}
function saveReadSet(){
  try { localStorage.setItem(READ_KEY, JSON.stringify([...readSet])); } catch(e){}
}
function markRead(it){
  readSet.add(paperKey(it));
  saveReadSet();
}

// ---- 숨긴 논문 (배터리 논문 아닌 것 걸러내기, localStorage) ----
const HIDE_KEY = "paperFeedHidden";
let hiddenSet = loadHiddenSet();
function loadHiddenSet(){
  try {
    const raw = localStorage.getItem(HIDE_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch(e){ return new Set(); }
}
function saveHiddenSet(){
  try { localStorage.setItem(HIDE_KEY, JSON.stringify([...hiddenSet])); } catch(e){}
}
function hidePaper(it){
  hiddenSet.add(paperKey(it));
  saveHiddenSet();
}
function unhideAll(){
  hiddenSet.clear();
  saveHiddenSet();
}
// 숨긴 논문 정보 사이드바 갱신
function updateHiddenInfo(){
  const info = document.getElementById("hiddenInfo");
  const countEl = document.getElementById("hiddenCount");
  if(!info) return;
  // 현재 피드에 실제로 존재하는 숨긴 논문 수만 셈
  const n = ITEMS.filter(it => hiddenSet.has(paperKey(it))).length;
  if(n > 0){
    info.hidden = false;
    countEl.textContent = `숨긴 논문 ${n}개`;
  } else {
    info.hidden = true;
  }
}

// ---- 사이드바 탭 만들기 ----
function buildTabs(){
  const tabs = document.getElementById("tabs");
  // 시스템/구성요소 필터를 반영한 카운트 (그룹 필터는 제외하고 셈)
  function countFor(group){
    return ITEMS.filter(it => {
      if(group !== "전체" && it.group !== group) return false;
      if(activeSystems.size>0 && !(it.systems||[]).some(s=>activeSystems.has(s))) return false;
      if(activeComponents.size>0 && !(it.components||[]).some(c=>activeComponents.has(c))) return false;
      if(searchQuery){
        const hay = (it.title + " " + it.abstract + " " + (it.authors||"")).toLowerCase();
        if(!hay.includes(searchQuery)) return false;
      }
      return true;
    }).length;
  }
  const list = ["전체", ...GROUPS];
  tabs.innerHTML = "";
  list.forEach(g => {
    const div = document.createElement("div");
    div.className = "tab" + (g===activeGroup ? " active":"");
    div.innerHTML = `<span>${g}</span><span class="count">${countFor(g)}</span>`;
    div.onclick = () => {
      activeGroup = g;
      backToFeed();         // 폴더 보기 - 메인 피드로
      render();
      buildTabs();
      buildFolders();
      // 모바일 드롭다운: 선택 시 라벨 갱신하고 접기
      document.getElementById("currentGroup").textContent = g;
      document.getElementById("sidebar").classList.remove("open");
    };
    tabs.appendChild(div);
  });
}

// ---- 폴더 목록 사이드바 ----
function buildFolders(){
  const box = document.getElementById("folderList");
  box.innerHTML = "";
  const names = Object.keys(folders);
  if(names.length === 0){
    const hint = document.createElement("div");
    hint.className = "folder-hint";
    hint.style.cssText = "font-size:12px; color:var(--muted); padding:6px 10px;";
    hint.textContent = "＋ 로 폴더를 만들어보세요";
    box.appendChild(hint);
    return;
  }
  names.forEach(name => {
    const div = document.createElement("div");
    const isActive = (viewMode==="folder" && activeFolder===name);
    div.className = "tab" + (isActive ? " active":"");
    div.innerHTML = `<span class="folder-name">📁 ${escAttr(name)}</span><span class="count">${folders[name].length}</span>`;
    // 폴더 선택
    div.querySelector(".folder-name").onclick = () => {
      viewMode = "folder"; activeFolder = name; activeGroup = "전체";
      render(); buildTabs(); buildFolders();
      document.getElementById("sidebar").classList.remove("open");
    };
    // 우클릭/길게 누르면 관리 메뉴 대신, 옆에 작은 메뉴 버튼
    const menu = document.createElement("span");
    menu.textContent = "⋯";
    menu.style.cssText = "cursor:pointer; color:var(--muted); padding:0 4px; margin-left:4px;";
    menu.onclick = (e) => { e.stopPropagation(); folderMenu(name); };
    div.appendChild(menu);
    box.appendChild(div);
  });
}

function folderMenu(name){
  const action = prompt(
    `폴더 "${name}"\n\n무엇을 할까요?\n  1 = 이름 수정\n  2 = 삭제\n\n번호를 입력하세요 (취소는 빈칸):`
  );
  if(action === "1"){
    const newName = prompt("새 폴더 이름:", name);
    if(newName && newName.trim() && newName !== name){
      if(folders[newName]){ alert("같은 이름의 폴더가 이미 있어요."); return; }
      folders[newName] = folders[name];
      delete folders[name];
      if(activeFolder===name) activeFolder=newName;
      saveFolders(); buildFolders(); render();
    }
  } else if(action === "2"){
    if(confirm(`폴더 "${name}"을(를) 삭제할까요? (담긴 논문 정보도 사라져요)`)){
      delete folders[name];
      if(activeFolder===name){ viewMode="feed"; activeFolder=null; }
      saveFolders(); buildFolders(); buildTabs(); render();
    }
  }
}

// + 폴더 추가
document.getElementById("addFolder").onclick = () => {
  const name = prompt("새 폴더 이름:");
  if(name && name.trim()){
    if(folders[name]){ alert("같은 이름의 폴더가 이미 있어요."); return; }
    folders[name] = [];
    saveFolders(); buildFolders();
  }
};

// 폴더 내보내기 (JSON 파일 다운로드)
document.getElementById("exportFolders").onclick = () => {
  if(Object.keys(folders).length === 0){ alert("내보낼 폴더가 없어요."); return; }
  const blob = new Blob([JSON.stringify(folders, null, 2)], {type:"application/json"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const today = new Date().toISOString().slice(0,10);
  a.href = url;
  a.download = `paper-folders-${today}.json`;
  a.click();
  URL.revokeObjectURL(url);
};

// 폴더 가져오기 (JSON 파일 업로드)
document.getElementById("importFolders").onclick = () => {
  document.getElementById("importFile").click();
};
document.getElementById("importFile").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = (ev) => {
    try {
      const imported = JSON.parse(ev.target.result);
      if(typeof imported !== "object" || Array.isArray(imported)) throw new Error("형식 오류");
      const mode = confirm(
        "가져온 폴더를 현재 폴더와 합칠까요?\\n\\n확인 = 합치기 (같은 이름은 내용 병합)\\n취소 = 현재 폴더를 모두 교체"
      );
      if(mode){
        // 병합
        for(const [name, papers] of Object.entries(imported)){
          if(!folders[name]) folders[name] = [];
          for(const p of papers){
            if(!folders[name].some(x => paperKey(x)===paperKey(p))) folders[name].push(p);
          }
        }
      } else {
        folders = imported;
      }
      saveFolders(); buildFolders(); buildTabs(); render();
      alert("폴더를 가져왔어요.");
    } catch(err){
      alert("파일을 읽을 수 없어요. 올바른 폴더 파일인지 확인해주세요.");
    }
  };
  reader.readAsText(file);
  e.target.value = "";  // 같은 파일 다시 선택 가능하게
});

// 메인 피드로 돌아가기 (출판사 그룹 클릭 시 자동)
function backToFeed(){
  viewMode = "feed"; activeFolder = null;
}
// ---- 날짜 선택기 ----
function updateDateUI(){
  const picker = document.getElementById("datePicker");
  const allChip = document.getElementById("dateAll");
  const selected = document.getElementById("dateSelected");
  // 선택 가능 범위를 데이터에 있는 날짜로 제한
  const dates = ITEMS.map(it => it.date).filter(d => d).sort();
  if(dates.length){
    picker.min = dates[0];
    picker.max = dates[dates.length-1];
  }
  // "전체" 활성 표시
  allChip.className = "chip" + (activeDate === "" ? " on" : "");
  // 선택된 날짜 표시
  if(activeDate){
    selected.hidden = false;
    selected.textContent = activeDate + " ✕";
    picker.value = activeDate;
  } else {
    selected.hidden = true;
    picker.value = "";
  }
}

// "전체" 클릭 → 날짜 해제
document.getElementById("dateAll").onclick = () => {
  activeDate = "";
  render(); buildChips(); buildTabs();
};
// 달력에서 날짜 선택
document.getElementById("datePicker").addEventListener("change", (e) => {
  activeDate = e.target.value || "";
  render(); buildChips(); buildTabs();
});
// 선택된 날짜 칩 클릭 → 해제
document.getElementById("dateSelected").onclick = () => {
  activeDate = "";
  render(); buildChips(); buildTabs();
};

function buildChips(){
  const sysBox = document.getElementById("systemChips");
  const compBox = document.getElementById("componentChips");
  sysBox.innerHTML = "";
  compBox.innerHTML = "";

  // 날짜 선택기 상태 갱신
  updateDateUI();

  SYSTEMS.forEach(s => {
    const c = document.createElement("span");
    c.className = "chip" + (activeSystems.has(s) ? " on":"");
    c.textContent = s;
    c.onclick = () => {
      activeSystems.has(s) ? activeSystems.delete(s) : activeSystems.add(s);
      render(); buildChips(); buildTabs();
    };
    sysBox.appendChild(c);
  });
  COMPONENTS.forEach(s => {
    const c = document.createElement("span");
    c.className = "chip" + (activeComponents.has(s) ? " on":"");
    c.textContent = s;
    c.onclick = () => {
      activeComponents.has(s) ? activeComponents.delete(s) : activeComponents.add(s);
      render(); buildChips(); buildTabs();
    };
    compBox.appendChild(c);
  });
}

// ---- 필터 적용: 같은 축 OR, 다른 축 AND ----
function passFilter(it){
  // 숨긴 논문 제외 (배터리 논문 아닌 것)
  if(hiddenSet.has(paperKey(it))) return false;
  // 출판사 그룹
  if(activeGroup !== "전체" && it.group !== activeGroup) return false;
  // 시스템 축 (선택된 게 있으면, 논문 시스템 중 하나라도 선택셋에 있어야 함 = OR)
  if(activeSystems.size > 0){
    if(!(it.systems||[]).some(s => activeSystems.has(s))) return false;
  }
  // 구성요소 축 (OR)
  if(activeComponents.size > 0){
    if(!(it.components||[]).some(c => activeComponents.has(c))) return false;
  }
  // 검색어 (제목/초록/저자)
  if(searchQuery){
    const hay = (it.title + " " + it.abstract + " " + (it.authors||"")).toLowerCase();
    if(!hay.includes(searchQuery)) return false;
  }
  // 날짜
  if(activeDate && it.date !== activeDate) return false;
  // 축들 사이는 AND (위 조건 전부 통과해야 도달)
  return true;
}

// ---- 모바일 드롭다운 펼침/접힘 ----
document.getElementById("dropdownToggle").onclick = () => {
  document.getElementById("sidebar").classList.toggle("open");
};

// ---- 필터 바 열고 닫기 (웹+모바일 공통) ----
function toggleFilter(){
  const fb = document.getElementById("filterbar");
  fb.hidden = !fb.hidden;
  // 모바일: 필터 열면 맨 위로 (필터바가 보이도록)
  if(!fb.hidden && window.innerWidth <= 680) window.scrollTo({top:0, behavior:"smooth"});
}
document.getElementById("filterToggle").onclick = toggleFilter;
{ const b = document.getElementById("filterToggleM"); if(b) b.onclick = toggleFilter; }

// ---- 검색 바 열고 닫기 ----
function toggleSearch(){
  const sb = document.getElementById("searchbar");
  sb.hidden = !sb.hidden;
  if(!sb.hidden){
    document.getElementById("searchInput").focus();
    if(window.innerWidth <= 680) window.scrollTo({top:0, behavior:"smooth"});
  }
}
document.getElementById("searchToggle").onclick = toggleSearch;
{ const b = document.getElementById("searchToggleM"); if(b) b.onclick = toggleSearch; }
document.getElementById("searchInput").addEventListener("input", (e) => {
  searchQuery = e.target.value.trim().toLowerCase();
  document.getElementById("searchToggle").textContent =
    searchQuery ? `🔍 검색 ●` : "🔍 검색";
  render(); buildTabs();
});
document.getElementById("searchClear").onclick = () => {
  document.getElementById("searchInput").value = "";
  searchQuery = "";
  document.getElementById("searchToggle").textContent = "🔍 검색";
  document.getElementById("searchbar").hidden = true;  // 검색바 닫기
  render(); buildTabs();
};

// ---- 필터 초기화 ----
document.getElementById("filterClear").onclick = () => {
  activeSystems.clear();
  activeComponents.clear();
  activeDate = "";
  render(); buildChips(); buildTabs();
};

// ---- 본문 렌더링 (날짜로 그룹핑) ----
function render(){
  const content = document.getElementById("content");

  // 필터 버튼에 활성 표시
  const nFilters = activeSystems.size + activeComponents.size + (activeDate ? 1 : 0);
  document.getElementById("filterToggle").textContent =
    nFilters > 0 ? `🔬 필터 (${nFilters})` : "🔬 필터";

  // 데이터 소스: 폴더 보기면 폴더 내용, 아니면 메인 피드
  let list;
  if(viewMode === "folder" && activeFolder !== null){
    list = (folders[activeFolder] || []).filter(it => {
      if(activeSystems.size>0 && !(it.systems||[]).some(s=>activeSystems.has(s))) return false;
      if(activeComponents.size>0 && !(it.components||[]).some(c=>activeComponents.has(c))) return false;
      if(searchQuery){
        const hay = (it.title + " " + it.abstract + " " + (it.authors||"")).toLowerCase();
        if(!hay.includes(searchQuery)) return false;
      }
      if(activeDate && it.date !== activeDate) return false;
      return true;
    });
  } else {
    list = ITEMS.filter(passFilter);
  }

  // 정렬: 최신 날짜 우선, 같은 날짜면 저널 이름 순
  list.sort((a,b) => (b.ts - a.ts) || a.journal.localeCompare(b.journal));

  if(list.length===0){
    const msg = (viewMode==="folder")
      ? '이 폴더가 비어있어요. 메인 피드에서 ☆ 를 눌러 논문을 담아보세요.'
      : '조건에 맞는 논문이 없어요.';
    content.innerHTML = `<p class="empty">${msg}</p>`;
    return;
  }

  let htmlStr = "";
  let lastDate = null;
  currentList = list;   // 저장 버튼 클릭 시 참조용
  for(let idx=0; idx<list.length; idx++){
    const it = list[idx];
    const d = it.date || "날짜 미상";
    if(d !== lastDate){
      htmlStr += `<div class="date-header">${d}</div>`;
      lastDate = d;
    }
    const abs = it.abstract
      ? `<p class="abstract">${esc(it.abstract)}</p>`
      : `<p class="abstract no-abstract">초록 없음 — 제목을 눌러 원문에서 확인</p>`;
    const sysTags = (it.systems||[]).map(s =>
      `<span class="cat-tag system" style="--sys-color:var(--sys-${s.replace(/[^a-zA-Z]/g,'')}, var(--muted))">${esc(s)}</span>`
    ).join("");
    const compTags = (it.components||[]).map(c =>
      `<span class="cat-tag component">${esc(c)}</span>`
    ).join("");
    const tagHtml = (sysTags || compTags)
      ? `<div class="cat-tags">${sysTags}${compTags}</div>` : "";
    // 그룹 색: CSS 변수 --g-<group> 을 카드의 --jcolor 로 연결
    const safeGroup = it.group.replace(/[^a-zA-Z]/g, "");
    // 저장 버튼: 폴더 보기면 빼기(x), 피드면 담기(*)
    const btnIcon = (viewMode==="folder") ? "✕" : "☆";
    const btnTitle = (viewMode==="folder") ? "이 폴더에서 빼기" : "폴더에 담기";
    const readClass = readSet.has(paperKey(it)) ? " read" : "";
    htmlStr += `
      <article class="card${readClass}" style="--jcolor:var(--g-${safeGroup}, var(--accent))">
        <div class="card-head">
          <span class="journal">${esc(it.journal)}</span>
          <div style="display:flex; align-items:center; gap:8px;">
            <span class="grp">${esc(it.group)}</span>
            <button class="claude-btn" data-idx="${idx}" title="Claude로 분석 (새 탭)">C</button>
            <button class="save-btn" data-idx="${idx}" title="${btnTitle}">${btnIcon}</button>
            ${viewMode!=="folder" ? `<button class="hide-btn" data-idx="${idx}" title="배터리 논문 아님 - 숨기기">🗑️</button>` : ""}
          </div>
        </div>
        <h2 class="title"><a href="${esc(it.link)}" data-idx="${idx}" target="_blank" rel="noopener">${esc(it.title)}</a></h2>
        ${it.authors ? `<p class="authors">${esc(it.authors)}</p>` : ""}
        ${abs}
        ${tagHtml}
      </article>`;
  }
  content.innerHTML = htmlStr;

  // 제목 클릭 → 읽음 처리
  content.querySelectorAll(".title a").forEach(a => {
    a.addEventListener("click", () => {
      const it = currentList[parseInt(a.dataset.idx, 10)];
      if(it){ markRead(it); a.closest(".card").classList.add("read"); }
    });
  });

  // Claude 분석 버튼 → claude.ai 탭 열기 (탭 재사용)
  content.querySelectorAll(".claude-btn").forEach(btn => {
    btn.onclick = () => {
      const it = currentList[parseInt(btn.dataset.idx, 10)];
      if(it){ markRead(it); btn.closest(".card").classList.add("read"); }
      // 같은 이름의 탭 재사용 → 매번 새 탭이 쌓이지 않음
      window.open("https://claude.ai/new", "claudeSummaryTab");
    };
  });

  // 숨기기 버튼 (배터리 논문 아닌 것)
  content.querySelectorAll(".hide-btn").forEach(btn => {
    btn.onclick = () => {
      const it = currentList[parseInt(btn.dataset.idx, 10)];
      if(!it) return;
      if(confirm(`이 논문을 피드에서 숨길까요?\\n\\n"${it.title.slice(0,60)}${it.title.length>60?"...":""}"\\n\\n(다시 빌드돼도 계속 안 보여요. 사이드바 아래 "숨긴 논문"에서 되살릴 수 있어요.)`)){
        hidePaper(it);
        render(); buildTabs(); buildFolders(); updateHiddenInfo();
      }
    };
  });

  // 저장 버튼 이벤트 (위임)
  content.querySelectorAll(".save-btn").forEach(btn => {
    btn.onclick = () => {
      const it = currentList[parseInt(btn.dataset.idx, 10)];
      if(!it) return;
      if(viewMode === "folder"){
        // 폴더에서 빼기
        folders[activeFolder] = (folders[activeFolder]||[]).filter(p => paperKey(p)!==paperKey(it));
        saveFolders(); render(); buildFolders();
      } else {
        // 폴더에 담기 (폴더 선택)
        savePaperToFolder(it);
      }
    };
  });
}

// 논문을 폴더에 담기
function savePaperToFolder(it){
  const names = Object.keys(folders);
  if(names.length === 0){
    if(confirm("폴더가 없어요. 새로 만들까요?")){
      const name = prompt("새 폴더 이름:");
      if(name && name.trim()){
        folders[name] = [];
      } else return;
    } else return;
  }
  const list = Object.keys(folders);
  let target;
  if(list.length === 1){
    target = list[0];
  } else {
    const choice = prompt(
      "어느 폴더에 담을까요?\\n\\n" +
      list.map((n,i)=>`  ${i+1} = ${n}`).join("\\n") +
      "\\n\\n번호 입력:"
    );
    const i = parseInt(choice,10)-1;
    if(isNaN(i) || i<0 || i>=list.length) return;
    target = list[i];
  }
  if(isSaved(target, it)){
    alert(`"${target}" 폴더에 이미 있어요.`);
    return;
  }
  folders[target] = folders[target] || [];
  folders[target].push(it);   // 논문 통째로 복사 저장
  saveFolders(); buildFolders();
  alert(`"${target}" 폴더에 담았어요. ⭐`);
}

function esc(s){
  return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
                .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function escAttr(s){ return esc(s); }

// ---- 라이트/다크 토글 (선택 기억) ----
const themeBtn = document.getElementById("themeBtn");
const themeBtnM = document.getElementById("themeBtnM");
function applyTheme(theme){
  document.documentElement.setAttribute("data-theme", theme);
  themeBtn.textContent = theme==="dark" ? "☀️ 라이트" : "🌙 다크";
  if(themeBtnM) themeBtnM.textContent = theme==="dark" ? "☀️" : "🌙";
}
// 저장된 테마 복원 (없으면 기본 라이트)
let savedTheme = "light";
try { savedTheme = localStorage.getItem("paperFeedTheme") || "light"; } catch(e){}
applyTheme(savedTheme);

function toggleTheme(){
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur==="dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("paperFeedTheme", next); } catch(e){}
}
themeBtn.onclick = toggleTheme;
if(themeBtnM) themeBtnM.onclick = toggleTheme;

buildTabs();
buildChips();
buildFolders();
updateHiddenInfo();
render();

// 숨긴 논문 되살리기
document.getElementById("restoreHidden").onclick = () => {
  if(confirm("숨긴 논문을 모두 되살릴까요?")){
    unhideAll();
    updateHiddenInfo();
    render(); buildTabs();
  }
};

// ---- 맨 위로 버튼 ----
const scrollTopBtn = document.getElementById("scrollTop");
window.addEventListener("scroll", () => {
  if(window.scrollY > 400) scrollTopBtn.classList.add("show");
  else scrollTopBtn.classList.remove("show");
});
scrollTopBtn.onclick = () => window.scrollTo({top:0, behavior:"smooth"});
</script>
</body>
</html>""".replace("__DATA__", data_json).replace("__GROUPS__", groups_json).replace("__SYSTEMS__", systems_json).replace("__COMPONENTS__", components_json).replace("__NOW__", now_str)


if __name__ == "__main__":
    main()
