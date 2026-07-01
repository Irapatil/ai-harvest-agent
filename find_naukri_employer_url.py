import asyncio
from playwright.async_api import async_playwright

async def find_employer_login_url():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto("https://www.naukri.com/nlogin/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        try:
            await page.locator("input[placeholder*='Email']").first.fill("irapratik@sightspectrum.com")
            await page.wait_for_timeout(500)
            await page.locator("input[type='password']").first.fill("Password@2026")
            await page.wait_for_timeout(500)
            await page.get_by_text("Login", exact=True).first.click()
            await page.wait_for_timeout(4000)
        except Exception as e:
            print("Submit error:", e)

        links = await page.evaluate("""
            () => {
                const anchors = Array.from(document.querySelectorAll('a'));
                return anchors
                    .filter(a => a.textContent.toLowerCase().includes('employer') ||
                                a.href.includes('employer'))
                    .map(a => ({text: a.textContent.trim(), href: a.href}));
            }
        """)
        print("Employer links after error:", links)

        errs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('[class*=error],[class*=err]'))
                       .map(e => e.innerText.trim()).filter(t => t.length > 0)
        """)
        print("Error texts:", errs[:5])

        # Also look for any anchor near error messages
        all_links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a'))
                       .map(a => ({text: a.textContent.trim(), href: a.href}))
                       .filter(a => a.href && a.href.startsWith('http'))
                       .slice(0, 20)
        """)
        print("All visible links:", all_links)

        await browser.close()

asyncio.run(find_employer_login_url())
