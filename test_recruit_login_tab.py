"""Click Register/Log in tab on recruit.naukri.com and inspect the login form."""
import asyncio
import sys
sys.path.insert(0, ".")

from app.config import get_settings
from app.scrapers.browser_manager import BrowserManager

async def test():
    cfg = get_settings()

    async with BrowserManager(headless=False, slow_mo=0) as bm:
        page = await bm.new_page()

        print("Navigating to recruit.naukri.com...")
        await page.goto("https://recruit.naukri.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        print("URL after load:", page.url)

        # Click the "Register/Log in" tab
        for sel in [
            "text=Register/Log in",
            "text=Register / Log in",
            "text=Login",
            "[class*='login']",
            "button:has-text('Login')",
            "a:has-text('Login')",
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    print(f"Clicking: {sel}")
                    await el.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        await page.screenshot(path="data/results/naukri/recruit_register_login_tab.png")
        print("URL after tab click:", page.url)

        # Find all inputs now
        inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input'))
                    .map(i => ({id: i.id, name: i.name, type: i.type,
                                placeholder: i.placeholder,
                                visible: i.offsetParent !== null}))
        """)
        print("Inputs after tab click:", inputs)

        # Try to fill credentials if email field visible
        for sel in ["input[placeholder*='Email']", "input[placeholder*='email']",
                    "input#usernameField", "input[type='email']", "input[name='email']",
                    "input[placeholder*='Username']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=3000):
                    print(f"Email field found: {sel}")
                    await el.fill(cfg.naukri_email)
                    await page.wait_for_timeout(400)

                    pwd = page.locator("input[type='password']").first
                    if await pwd.is_visible(timeout=3000):
                        await pwd.fill(cfg.naukri_password)
                        await page.wait_for_timeout(400)
                        await page.screenshot(path="data/results/naukri/recruit_credentials_filled.png")
                        await page.get_by_text("Login", exact=True).first.click()
                        await page.wait_for_timeout(5000)
                        await page.screenshot(path="data/results/naukri/recruit_after_login.png")
                        print("After login URL:", page.url)
                    break
            except Exception:
                continue

asyncio.run(test())
