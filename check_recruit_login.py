"""Check what the Naukri /recruit/login page looks like."""
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
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        print("Navigating to /recruit/login...")
        resp = await page.goto("https://www.naukri.com/recruit/login", wait_until="domcontentloaded")
        print(f"Status: {resp.status}, Final URL: {page.url}")
        await page.wait_for_timeout(3000)

        await page.screenshot(path="data/results/naukri/recruit_login_page.png")

        # Find form inputs
        inputs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input'))
                    .map(i => ({
                        id: i.id, name: i.name, type: i.type,
                        placeholder: i.placeholder, visible: i.offsetParent !== null
                    }))
        """)
        print("Input fields:", inputs)

        # Try to fill credentials and submit
        print("\nFilling credentials...")
        try:
            await page.locator("input[placeholder*='Email'], input[placeholder*='Username'], input#usernameField").first.fill("irapratik@sightspectrum.com")
            await page.wait_for_timeout(400)
            await page.locator("input[type='password']").first.fill("Password@2026")
            await page.wait_for_timeout(400)
            await page.screenshot(path="data/results/naukri/recruit_before_submit.png")
            await page.get_by_text("Login", exact=True).first.click()
            await page.wait_for_timeout(5000)
        except Exception as e:
            print("Error:", e)

        await page.screenshot(path="data/results/naukri/recruit_after_submit.png")
        print("After submit URL:", page.url)

        await browser.close()

asyncio.run(check())
