"""Disposable Bookker browser and browser-only input for the renewal page."""

import os
from contextlib import AsyncExitStack

from parking_booking import (
    BOOKING_LIST_ROWS, MADRID, NO_BOOKINGS, is_sign_in_route, navigation_links,
    same_origin, sign_in_controls, visible,
)

WIDTH, HEIGHT = 420, 690
KEYS = {"Backspace", "Enter", "Tab"}


class SignInIncomplete(Exception):
    pass


async def launch_browser(stack):
    from playwright.async_api import async_playwright

    driver = async_playwright()
    stack.push_async_exit(driver)
    playwright = await driver.start()
    browser = await playwright.chromium.launch(
        channel=os.environ.get("PLAYWRIGHT_CHANNEL") or None,
        chromium_sandbox=True, timeout=15_000,
    )
    stack.push_async_callback(browser.close)
    return browser


class BrowserViewer:
    def __init__(self, base_url):
        self.base_url = base_url
        self.stack = AsyncExitStack()

    async def start(self):
        browser = await launch_browser(self.stack)
        self.context = await browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT}, is_mobile=True, has_touch=True,
            timezone_id=MADRID, accept_downloads=False,
        )
        self.stack.push_async_callback(self.context.close)
        self.context.set_default_timeout(3000)
        page = await self.context.new_page()
        await page.goto(self.base_url, wait_until="domcontentloaded", timeout=15_000)

    @property
    def page(self):
        return self.context.pages[-1]

    async def frame(self):
        return await self.page.screenshot(type="jpeg", quality=65, timeout=2000)

    async def input(self, action):
        page = self.page
        if "tap" in action:
            await page.touchscreen.tap(*action["tap"])
        elif "text" in action:
            await page.keyboard.insert_text(action["text"])
        elif "key" in action:
            await page.keyboard.press(action["key"])
        elif "scroll" in action:
            await page.mouse.move(WIDTH // 2, HEIGHT // 2)
            await page.mouse.wheel(0, action["scroll"])

    async def candidate(self):
        return await self.context.storage_state(indexed_db=True)

    async def close(self):
        await self.stack.aclose()


async def validate_session(base_url, state):
    """Use a separate browser and a fresh context seeded only with the candidate."""
    async with AsyncExitStack() as stack:
        browser = await launch_browser(stack)
        context = await browser.new_context(storage_state=state, timezone_id=MADRID, accept_downloads=False)
        stack.push_async_callback(context.close)
        page = await context.new_page()
        await page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
        if not await authenticated(page, base_url):
            raise SignInIncomplete
        link, route = navigation_links(page, "Bookings")
        view = visible(page.locator("app-bookings").or_(page.get_by_role("main")))
        records = visible(view.locator(BOOKING_LIST_ROWS).or_(view.get_by_text(NO_BOOKINGS)))
        try:
            await visible(link.or_(route)).first.click(timeout=5000)
            await records.first.wait_for(timeout=5000)
        except Exception:
            raise SignInIncomplete from None
        if not await authenticated(page, base_url):
            raise SignInIncomplete


async def authenticated(page, base_url):
    return (same_origin(page.url, base_url) and not is_sign_in_route(page.url)
            and not await sign_in_controls(page).count())
