"""Test recruit.naukri.com login using BrowserManager (stealth mode)."""
import asyncio
import sys
sys.path.insert(0, ".")

from app.config import get_settings
from app.scrapers.browser_manager import BrowserManager

async def test():
    cfg = get_settings()
    print(f"Email: {cfg.naukri_email}")

    async with BrowserManager(headless=False, slow_mo=0) as bm:
        page = await bm.new_page()

        print("Navigating to recruit.naukri.com...")
        resp = await page.goto("https://recruit.naukri.com/", wait_until="domcontentloaded")
        print(f"Status: {resp.status if resp else 'N/A'}, Final URL: {page.url}")
        await page.wait_for_timeout(4000)
        await page.screenshot(path="data/results/naukri/stealth_recruit_home.png")

        inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input'))
                    .map(i => ({id: i.id, name: i.name, type: i.type,
                                placeholder: i.placeholder,
                                visible: i.offsetParent !== null}))
        """)
        print("Inputs found:", inputs)
        print("Current URL:", page.url)

        # If login form visible, fill credentials
        email_visible = False
        for sel in ["input#usernameField", "input[placeholder*='Email']", "input[placeholder*='Username']", "input[type='email']", "input[name='email']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    email_visible = True
                    print(f"Login form found via: {sel}")
                    await el.fill(cfg.naukri_email)
                    await page.wait_for_timeout(400)
                    await page.locator("input[type='password']").first.fill(cfg.naukri_password)
                    await page.wait_for_timeout(400)
                    await page.screenshot(path="data/results/naukri/stealth_before_submit.png")
                    await page.get_by_text("Login", exact=True).first.click()
                    await page.wait_for_timeout(5000)
                    await page.screenshot(path="data/results/naukri/stealth_after_submit.png")
                    print("After submit URL:", page.url)
                    break
            except Exception:
                continue

        if not email_visible:
            print("No standard login form found — checking for alternative login links")
            links = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a'))
                        .filter(a => a.href && (a.href.includes('login') ||
                                     a.textContent.toLowerCase().includes('login') ||
                                     a.textContent.toLowerCase().includes('sign in')))
                        .map(a => ({text: a.textContent.trim(), href: a.href}))
            """)
            print("Login links:", links)

asyncio.run(test())
