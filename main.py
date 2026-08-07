#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
오즈 공고봇 — 매일 오전 8시(KST) 지원사업·공모 수집 → 카카오톡 「나에게 보내기」

수집   네이버 검색 API(웹문서·뉴스) + 기업마당
판정   오즈 적합도 점수화 → 🔴즉시 / 🟡검토 / ⬜참고
저장   docs/index.html (GitHub Pages) + docs/archive/YYYY-MM-DD.html
발송   카카오 메모 API (리스트 템플릿, 상위 3건)

환경변수 (GitHub Secrets)
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET
  KAKAO_REST_KEY  / KAKAO_REFRESH_TOKEN
  PAGES_URL        예) https://아이디.github.io/oz-brief
"""
import os, re, json, html, pathlib, datetime, urllib.parse
import requests
from bs4 import BeautifulSoup

KST = datetime.timezone(datetime.timedelta(hours=9))
TODAY = datetime.datetime.now(KST).date()
ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"
SEEN = ROOT / "seen.json"

NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
KAKAO_KEY = os.environ.get("KAKAO_REST_KEY", "")
KAKAO_REFRESH = os.environ.get("KAKAO_REFRESH_TOKEN", "")
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")

# ─────────────────────────────────────────────────────────────
# 키워드
# ─────────────────────────────────────────────────────────────
QUERIES = [
    "진로체험 업체 공모", "진로직업체험 캠프 업체", "방문형 진로체험 공모",
    "진로체험처 모집", "자유학기제 프로그램 공모", "진로교육 위탁 공고",
    "사회적기업 지원사업 공고", "장애인기업 우대 공고",
    "경기도교육청 진로체험", "시흥 교육 지원사업",
    "학교 초청공연 입찰", "청소년 진로 프로그램 공모",
]

# 점수 규칙 (키워드, 가점, 사유)
SCORE_RULES = [
    (r"진로체험|진로직업|직업체험|진로캠프", 40, "오즈 주력 분야"),
    (r"업체\s*공모|업체\s*모집|위탁|입찰|용역", 30, "발주 건"),
    (r"지원사업|참여기업|모집\s*공고|참가기업|선정\s*공고", 20, "지원사업"),
    (r"사회적기업", 30, "오즈 = 사회적기업"),
    (r"장애인기업|중증장애인", 30, "오즈 = 장애인기업"),
    (r"교육청|교육지원청|교육부", 25, "공공 발주"),
    (r"경기|시흥|안산|광명|부천", 20, "오즈 소재 지역"),
    (r"학교|초등|중학교|고등학교|청소년", 15, "오즈 고객"),
    (r"공연|뮤지컬|마술|문화예술", 15, "오즈 상품"),
    (r"교육기부|진로교육", 15, "오즈 인증 분야"),
]

EXCLUDE = re.compile(
    r"채용|구인|인턴|아르바이트|수강생\s*모집|학생\s*모집|"
    r"대학생\s*대상|창업경진대회|공모전\s*수상|입상|당선작|"
    r"장학금|졸업|입시|학원생"
)


# ─────────────────────────────────────────────────────────────
# 수집
# ─────────────────────────────────────────────────────────────
def naver_search(query, kind="webkr", display=10):
    if not NAVER_ID:
        return []
    url = f"https://openapi.naver.com/v1/search/{kind}.json"
    try:
        r = requests.get(
            url,
            headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
            params={"query": query, "display": display, "sort": "date" if kind == "news" else "sim"},
            timeout=15,
        )
        r.raise_for_status()
        out = []
        for it in r.json().get("items", []):
            out.append({
                "title": strip_tags(it.get("title", "")),
                "desc": strip_tags(it.get("description", "")),
                "url": it.get("link", ""),
                "src": f"네이버 {'뉴스' if kind=='news' else '웹'}",
                "deadline": "",
            })
        return out
    except Exception as e:
        print(f"[warn] naver {kind} '{query}': {e}")
        return []


def bizinfo():
    """기업마당 지원사업 공고 1페이지"""
    url = "https://www.bizinfo.go.kr/web/lay1/bbs/S1T122C128/AS/74/list.do"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for tr in soup.select("table tbody tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.select("td")]
            a = tr.select_one("a[href]")
            if len(tds) < 4 or not a:
                continue
            href = a.get("href", "")
            link = href if href.startswith("http") else urllib.parse.urljoin(url, href)
            title = a.get_text(" ", strip=True)
            period = next((t for t in tds if re.search(r"\d{4}-\d{2}-\d{2}", t)), "")
            org = next((t for t in tds if re.search(r"부|청|원|공단|진흥|도|시$", t) and len(t) < 30), "")
            out.append({
                "title": title, "desc": org, "url": link,
                "src": "기업마당", "deadline": period,
            })
        return out
    except Exception as e:
        print(f"[warn] bizinfo: {e}")
        return []


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ─────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────
def days_left(deadline):
    """접수기간 문자열에서 마감일 추출 → 남은 일수"""
    ds = re.findall(r"(\d{4})-(\d{2})-(\d{2})", deadline or "")
    if not ds:
        return None
    y, m, d = ds[-1]
    try:
        return (datetime.date(int(y), int(m), int(d)) - TODAY).days
    except ValueError:
        return None


def judge(item):
    text = f"{item['title']} {item['desc']}"
    if EXCLUDE.search(text):
        return None
    score, reasons = 0, []
    for pat, pts, why in SCORE_RULES:
        if re.search(pat, text):
            score += pts
            reasons.append(why)
    if score < 40:
        return None

    dl = days_left(item.get("deadline", ""))
    if dl is not None:
        if dl < 0:
            return None                     # 이미 마감
        if dl <= 7:
            score += 25
            reasons.append(f"마감 {dl}일 전")
        elif dl <= 14:
            score += 10

    item["score"] = score
    item["days_left"] = dl
    item["why"] = " · ".join(dict.fromkeys(reasons))
    item["grade"] = "🔴" if score >= 85 else ("🟡" if score >= 60 else "⬜")
    return item


# ─────────────────────────────────────────────────────────────
# 중복 제거
# ─────────────────────────────────────────────────────────────
def load_seen():
    if SEEN.exists():
        try:
            return set(json.loads(SEEN.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_seen(seen):
    SEEN.write_text(json.dumps(sorted(seen)[-4000:], ensure_ascii=False, indent=0), encoding="utf-8")


def key_of(item):
    return re.sub(r"\s+", "", item["title"])[:60]


# ─────────────────────────────────────────────────────────────
# 출력 — HTML
# ─────────────────────────────────────────────────────────────
CSS = """
body{font-family:-apple-system,'Malgun Gothic',sans-serif;max-width:760px;margin:0 auto;
padding:24px 16px 64px;line-height:1.65;color:#222;background:#fafafa}
h1{font-size:22px;margin:0 0 4px}.sub{color:#888;font-size:13px;margin-bottom:24px}
.card{background:#fff;border:1px solid #e6e6e6;border-radius:12px;padding:16px 18px;margin-bottom:12px}
.card.hot{border-color:#ff5a5a;border-width:2px}
.g{font-size:12px;font-weight:700;letter-spacing:.5px}
.t{font-size:16px;font-weight:700;margin:6px 0;word-break:keep-all}
.t a{color:#111;text-decoration:none}.t a:hover{text-decoration:underline}
.m{font-size:13px;color:#666}.why{font-size:13px;color:#0a7;margin-top:6px}
.dl{display:inline-block;background:#fff0f0;color:#d33;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700}
.sec{margin:28px 0 10px;font-size:15px;font-weight:700;color:#555}
.none{color:#999;padding:24px;text-align:center;background:#fff;border-radius:12px}
"""


def render(items):
    n = {"🔴": 0, "🟡": 0, "⬜": 0}
    for i in items:
        n[i["grade"]] += 1
    parts = [
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>오즈 공고 브리핑 {TODAY}</title><style>{CSS}</style></head><body>",
        f"<h1>오즈 공고 브리핑</h1>",
        f"<div class='sub'>{TODAY} · 🔴 즉시 {n['🔴']}건 · 🟡 검토 {n['🟡']}건 · ⬜ 참고 {n['⬜']}건</div>",
    ]
    if not items:
        parts.append("<div class='none'>오늘 새로 올라온 공고가 없습니다.</div>")
    for grade, label in [("🔴", "즉시 검토"), ("🟡", "검토 대상"), ("⬜", "참고")]:
        group = [i for i in items if i["grade"] == grade]
        if not group:
            continue
        parts.append(f"<div class='sec'>{grade} {label} ({len(group)})</div>")
        for i in group:
            dl = ""
            if i.get("days_left") is not None:
                dl = f"<span class='dl'>마감 {i['days_left']}일 전</span> "
            parts.append(
                f"<div class='card{' hot' if grade=='🔴' else ''}'>"
                f"<div class='g'>{grade} {i['score']}점 · {html.escape(i['src'])}</div>"
                f"<div class='t'><a href='{html.escape(i['url'])}' target='_blank' rel='noopener'>"
                f"{html.escape(i['title'])}</a></div>"
                f"<div class='m'>{dl}{html.escape(i.get('deadline') or i.get('desc',''))[:120]}</div>"
                f"<div class='why'>▸ {html.escape(i['why'])}</div></div>"
            )
    parts.append("</body></html>")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# 발송 — 카카오
# ─────────────────────────────────────────────────────────────
def kakao_token():
    r = requests.post("https://kauth.kakao.com/oauth/token", timeout=15, data={
        "grant_type": "refresh_token",
        "client_id": KAKAO_KEY,
        "refresh_token": KAKAO_REFRESH,
    })
    r.raise_for_status()
    j = r.json()
    if "refresh_token" in j:
        print("[!] 새 refresh_token 발급됨 — GitHub Secrets 갱신 필요:", j["refresh_token"][:12] + "...")
    return j["access_token"]


def kakao_send(items):
    """리스트 템플릿: 상위 3건. 링크는 전부 PAGES_URL (카카오 도메인 제한 회피)"""
    if not (KAKAO_KEY and KAKAO_REFRESH and PAGES_URL):
        print("[skip] 카카오 설정 없음")
        return
    top = [i for i in items if i["grade"] in ("🔴", "🟡")][:3]
    if not top:
        print("[skip] 보고할 건 없음 — 카톡 미발송")
        return

    n = sum(1 for i in items if i["grade"] == "🔴")
    link = {"web_url": PAGES_URL, "mobile_web_url": PAGES_URL}
    contents = [{
        "title": f"{i['grade']} {i['title'][:38]}",
        "description": (f"마감 {i['days_left']}일 전 · " if i.get("days_left") is not None else "") + i["why"][:40],
        "image_url": "https://t1.kakaocdn.net/kakaocorp/Service/KakaoTalk/pc/slide/talkpc_theme_01.jpg",
        "link": link,
    } for i in top]
    while len(contents) < 2:                       # 리스트 템플릿은 2개 이상 필수
        contents.append({"title": "전체 브리핑 보기", "description": "오늘 수집한 전체 목록",
                         "image_url": contents[0]["image_url"], "link": link})

    payload = {
        "object_type": "list",
        "header_title": f"[오즈 {TODAY.month}/{TODAY.day}] 즉시 {n}건 · 총 {len(items)}건",
        "header_link": link,
        "contents": contents,
        "buttons": [{"title": "전체 브리핑 열기", "link": link}],
    }
    r = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {kakao_token()}"},
        data={"template_object": json.dumps(payload, ensure_ascii=False)},
        timeout=15,
    )
    print("[kakao]", r.status_code, r.text[:200])


# ─────────────────────────────────────────────────────────────
def main():
    raw = []
    for q in QUERIES:
        raw += naver_search(q, "webkr", 10)
    for q in QUERIES[:6]:
        raw += naver_search(q, "news", 5)
    raw += bizinfo()
    print(f"[수집] {len(raw)}건")

    seen = load_seen()
    items, new_keys = [], set()
    for it in raw:
        k = key_of(it)
        if k in seen or k in new_keys or not it.get("url"):
            continue
        j = judge(it)
        if j:
            items.append(j)
            new_keys.add(k)
    items.sort(key=lambda x: -x["score"])
    items = items[:25]
    print(f"[판정] 신규 {len(items)}건")

    DOCS.mkdir(exist_ok=True)
    (DOCS / "archive").mkdir(exist_ok=True)
    page = render(items)
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (DOCS / "archive" / f"{TODAY}.html").write_text(page, encoding="utf-8")

    kakao_send(items)
    save_seen(seen | {key_of(i) for i in items})
    print("[완료]")


if __name__ == "__main__":
    main()
