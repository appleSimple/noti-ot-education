import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = "targets.json"
STATE_FILE = "state.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (notice-watcher; +https://github.com/)"
}

@dataclass
class Item:
    item_id: str
    title: str
    url: str

def load_config() -> Dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state() -> Dict[str, Set[str]]:
    """
    state.json 형태:
    {
      "cogsociety_notice": ["12345", "12344", ...],
      "other_target": ["..."]
    }
    """
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: set(map(str, v)) for k, v in raw.items()}
    except Exception:
        return {}

def save_state(state: Dict[str, Set[str]]):
    # 너무 커지지 않게 target별 3000개 제한
    compact = {k: list(sorted(v, reverse=True))[:3000] for k, v in state.items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False, indent=2)

def telegram_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN / CHAT_ID 환경변수가 비어 있습니다. (GitHub Secrets 확인)")

    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(api, json=payload, timeout=20)
    r.raise_for_status()

def normalize_url(href: str, base: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return urljoin(base, href)
    return urljoin(base + "/", href)

def parse_html_key_list(target_url: str, key_pattern: str, latest_n: int) -> List[Item]:
    """
    목록 페이지에서 key_pattern이 포함된 a[href]를 찾아 item_id(숫자/키), title, url 추출.
    예: key_pattern="view.asp?Key=" -> view.asp?Key=12345 를 찾아 12345 추출
    """
    r = requests.get(target_url, headers=HEADERS, timeout=25)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    # base는 scheme+netloc만 사용(상대경로 결합)
    u = urlparse(target_url)
    base = f"{u.scheme}://{u.netloc}"

    # key_pattern 뒤에 오는 값을 id로 뽑음 (숫자만이 아니어도 대응)
    # 예: view.asp?Key=12345  또는 ...Key=abc123
    key_re = re.compile(re.escape(key_pattern) + r"([^&#]+)", re.IGNORECASE)

    items_by_id: Dict[str, Item] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = key_re.search(href)
        if not m:
            continue

        item_id = m.group(1).strip()
        title = a.get_text(strip=True)
        if not title:
            continue

        full_url = normalize_url(href, base)
        items_by_id[item_id] = Item(item_id=item_id, title=title, url=full_url)

    # id가 숫자면 숫자 기준으로 내림차순 정렬(최신일 가능성 높음), 아니면 문자열 정렬
    def sort_key(it: Item):
        return int(it.item_id) if it.item_id.isdigit() else it.item_id

    items = sorted(items_by_id.values(), key=sort_key, reverse=True)
    return items[:latest_n]

def run_target(target: Dict, state: Dict[str, Set[str]]):
    name = target["name"]
    url = target["url"]
    ttype = target.get("type", "html_key_list")
    latest_n = int(target.get("latest_n", 30))

    seen = state.get(name, set())

    if ttype == "html_key_list":
        key_pattern = target.get("key_pattern", "view.asp?Key=")
        items = parse_html_key_list(url, key_pattern, latest_n)
    else:
        raise ValueError(f"Unsupported target type: {ttype}")

    new_items = [it for it in items if it.item_id not in seen]
    if not new_items:
        print(f"[{name}] No new items.")
        return

    # 오래된 것부터 보내고 싶으면 reverse=True/False 조정
    def sort_key(it: Item):
        return int(it.item_id) if it.item_id.isdigit() else it.item_id

    new_items.sort(key=sort_key)

    for it in new_items:
        msg = f"🆕 새 글 ({name})\n- {it.title}\n- {it.url}"
        telegram_send(msg)
        print(f"[{name}] Sent: {it.item_id} {it.title}")
        seen.add(it.item_id)
        time.sleep(0.7)  # 텔레그램/사이트에 부담 줄이기

    state[name] = seen

def main():
    config = load_config()
    targets = config.get("targets", [])
    if not targets:
        raise RuntimeError("targets.json에 targets가 비어 있습니다.")

    state = load_state()

    for target in targets:
        try:
            run_target(target, state)
        except Exception as e:
            # 한 타겟 실패가 전체 중단으로 이어지지 않게
            err_msg = f"⚠️ 크롤러 오류 ({target.get('name','unknown')})\n- {type(e).__name__}: {e}"
            print(err_msg)
            # 필요하면 오류도 텔레그램으로 보내고 싶을 때 아래 주석 해제
            # telegram_send(err_msg)

    save_state(state)

if __name__ == "__main__":
    main()