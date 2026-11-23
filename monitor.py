import asyncio
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


BASE_URL = "https://www.avito.ru"
SEARCH_URL = os.environ.get("SEARCH_URL", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
MAX_PRICE = int(os.environ.get("MAX_PRICE", "35000"))
SEEN_FILE = Path(os.environ.get("SEEN_FILE", "seen.json"))
DEBUG_DIR = Path("debug")

# Список можно расширить через переменную BANNED_PHRASES, разделитель — |.
DEFAULT_BANNED_PHRASES = [
    "без документов",
    "нет документов",
    "документы отсутствуют",
    "документы утеряны",
    "утеряны документы",
    "битая",
    "битый",
    "битое",
    "после дтп",
    "на запчасти",
    "под разбор",
    "в разбор",
    "утиль",
    "не на ходу",
    "требует восстановления",
]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_url(value: str) -> str:
    absolute = urljoin(BASE_URL, value)
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_price(value: str) -> int | None:
    match = re.search(r"(\d[\d\s\u00a0]{0,12})\s*₽", value or "")
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def banned_phrases() -> list[str]:
    custom = os.environ.get("BANNED_PHRASES", "").strip()
    values = custom.split("|") if custom else DEFAULT_BANNED_PHRASES
    return [normalize_space(item).casefold() for item in values if normalize_space(item)]


def rejection_reason(text: str) -> str | None:
    folded = normalize_space(text).casefold().replace("ё", "е")
    for phrase in banned_phrases():
        candidate = phrase.replace("ё", "е")
        if candidate in folded:
            return phrase
    return None


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    # Ограничиваем историю, чтобы файл не рос бесконечно.
    SEEN_FILE.write_text(
        json.dumps(sorted(seen)[-5000:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def telegram_chunks(title: str, price: int, url: str, description: str) -> list[str]:
    header = (
        f"<b>{html.escape(title)}</b>\n"
        f"Цена: <b>{price:,} ₽</b>\n"
        f'<a href="{html.escape(url, quote=True)}">Открыть объявление</a>\n\n'
    ).replace(",", " ")
    body = html.escape(description or "Описание отсутствует")
    limit = 3900
    chunks: list[str] = []
    first_room = limit - len(header)
    chunks.append(header + body[:first_room])
    body = body[first_room:]
    while body:
        chunks.append(body[:limit])
        body = body[limit:]
    return chunks


def send_telegram(title: str, price: int, url: str, description: str) -> None:
    endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for index, chunk in enumerate(telegram_chunks(title, price, url, description), start=1):
        response = requests.post(
            endpoint,
            json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": index != 1,
            },
            timeout=30,
        )
        response.raise_for_status()


async def text_from_first(page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                value = normalize_space(await locator.inner_text(timeout=3000))
                if value:
                    return value
        except PlaywrightTimeoutError:
            pass
    return ""


async def collect_links(page) -> list[str]:
    await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_selector('[data-marker="item"]', timeout=20000)
    except PlaywrightTimeoutError:
        DEBUG_DIR.mkdir(exist_ok=True)
        await page.screenshot(path=str(DEBUG_DIR / "search-page.png"), full_page=True)
        raise RuntimeError(
            "Avito не показал список объявлений. Возможны CAPTCHA/блокировка IP; "
            "смотрите debug/search-page.png в артефактах запуска."
        )

    cards = page.locator('[data-marker="item"]')
    links: list[str] = []
    for i in range(await cards.count()):
        card = cards.nth(i)
        anchor = card.locator('a[href*="/avtomobili/"]').first
        if not await anchor.count():
            continue
        href = await anchor.get_attribute("href")
        if not href:
            continue
        url = normalized_url(href)
        if url not in links:
            links.append(url)
    return links


async def inspect_listing(context, url: str) -> dict | None:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        title = await text_from_first(page, [
            '[data-marker="item-view/title-info"]',
            'h1[data-marker="item-view/title-info"]',
            "h1",
        ])
        price_text = await text_from_first(page, [
            '[data-marker="item-view/item-price"]',
            '[itemprop="price"]',
        ])
        description = await text_from_first(page, [
            '[data-marker="item-view/item-description"]',
            '[itemprop="description"]',
        ])
        all_text = "\n".join([title, price_text, description])
        price = parse_price(price_text or all_text)
        if not title or price is None:
            print(f"SKIP: не удалось прочитать заголовок/цену: {url}")
            return None
        if price > MAX_PRICE:
            print(f"SKIP: цена {price} > {MAX_PRICE}: {url}")
            return None
        reason = rejection_reason(all_text)
        if reason:
            print(f"SKIP: запрещённая фраза «{reason}»: {url}")
            return None
        return {"title": title, "price": price, "description": description, "url": url}
    except PlaywrightTimeoutError:
        print(f"SKIP: тайм-аут страницы: {url}")
        return None
    finally:
        await page.close()


async def main() -> None:
    missing = [
        name for name, value in {
            "SEARCH_URL": SEARCH_URL,
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_CHAT_ID": CHAT_ID,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError("Не заданы переменные: " + ", ".join(missing))

    seen = load_seen()
    accepted = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Saratov",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        links = await collect_links(page)
        await page.close()
        print(f"Найдено ссылок: {len(links)}; уже просмотрено: {len(seen)}")

        for url in links:
            if url in seen:
                continue
            # Помечаем просмотренным даже неподходящее объявление: повторно его проверять не нужно.
            seen.add(url)
            listing = await inspect_listing(context, url)
            if listing:
                send_telegram(**listing)
                accepted += 1
                print(f"SENT: {listing['title']} — {listing['price']} ₽")
            await asyncio.sleep(1.5)

        await browser.close()

    save_seen(seen)
    print(f"Готово. Отправлено новых объявлений: {accepted}")


if __name__ == "__main__":
    asyncio.run(main())

