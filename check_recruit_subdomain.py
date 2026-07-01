"""Inspect the recruit.naukri.com login page and test credentials."""
import asyncio
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        print("Navigating to recruit.naukri.com...")
        resp = await page.goto("https://recruit.naukri.com/", wait_until="domcontentloaded")
        print(f"Status: {resp.status if resp else 'N/A'}, Final URL: {page.url}")
        await page.wait_for_timeout(4000)
        await page.screenshot(path="data/results/naukri/recruit_subdomain_home.png")

        # List all inputs
        inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input'))
                    .map(i => ({id: i.id, name: i.name, type: i.type,
                                placeholder: i.placeholder,
                                visible: i.offsetParent !== null}))
        """)
        print("Input fields on home:", inputs)

        # List navigation links
        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a'))
                    .filter(a => a.href && (a.href.includes('login') || a.href.includes('signin') ||
                                            a.textContent.toLowerCase().includes('login') ||
                                            a.textContent.toLowerCase().includes('sign in')))
                    .map(a => ({text: a.textContent.trim(), href: a.href}))
        """)
        print("Login-related links:", links)

        # Try to find login form or navigate to login
        login_url = page.url
        if "login" not in page.url.lower():
            # Try clicking Login button
            try:
                await page.get_by_text("Login", exact=False).first.click()
                await page.wait_for_timeout(3000)
                print("After Login click URL:", page.url)
                await page.screenshot(path="data/results/naukri/recruit_after_login_click.png")
            except Exception as e:
                print("Login click error:", e)

        # Check inputs again
        inputs2 = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input'))
                    .map(i => ({id: i.id, name: i.name, type: i.type,
                                placeholder: i.placeholder,
                                visible: i.offsetParent !== null}))
        """)
        print("Input fields after nav:", inputs2)
        print("Current URL:", page.url)

        await browser.close()

asyncio.run(check())
