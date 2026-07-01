"""Find the actual Naukri employer/recruiter login URL via the For Employers dropdown."""
import asyncio
from playwright.async_api import async_playwright

async def find():
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

        print("Navigating to naukri.com...")
        await page.goto("https://www.naukri.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Find and hover "For employers" link
        for_emp = page.get_by_text("For employers", exact=False).first
        if await for_emp.is_visible(timeout=5000):
            print("Found 'For employers' - hovering...")
            await for_emp.hover()
            await page.wait_for_timeout(1500)

            # Screenshot to see the dropdown
            await page.screenshot(path="data/results/naukri/employer_dropdown.png")

            # Find all links in the dropdown
            dropdown_links = await page.evaluate("""
                () => {
                    return Array.from(document.querySelectorAll('a'))
                        .filter(a => a.href.includes('employer') || a.href.includes('recruiter') ||
                                     a.textContent.toLowerCase().includes('employer') ||
                                     a.textContent.toLowerCase().includes('recruiter') ||
                                     a.textContent.toLowerCase().includes('login'))
                        .map(a => ({text: a.textContent.trim(), href: a.href}))
                        .filter(a => a.text.length > 0 && a.href.startsWith('http'))
                }
            """)
            print("Employer/recruiter links found:")
            for link in dropdown_links:
                print(f"  [{link['text']}] -> {link['href']}")
        else:
            print("'For employers' not found")

        # Also try the error message link approach
        print("\nNavigating to mnjuser/employerlogin to trigger error...")
        await page.goto("https://www.naukri.com/mnjuser/employerlogin", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        print("Current URL after redirect:", page.url)

        # Fill and submit
        try:
            await page.locator("input[placeholder*='Email']").first.fill("irapratik@sightspectrum.com")
            await page.wait_for_timeout(400)
            await page.locator("input[type='password']").first.fill("Password@2026")
            await page.wait_for_timeout(400)
            await page.get_by_text("Login", exact=True).first.click()
            await page.wait_for_timeout(4000)
        except Exception as e:
            print("Submit error:", e)

        await page.screenshot(path="data/results/naukri/employer_login_error.png")

        # Read the employer login link from error
        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a'))
                    .filter(a => a.textContent.toLowerCase().includes('employer'))
                    .map(a => ({text: a.textContent.trim(), href: a.href}))
        """)
        print("Employer links on error page:")
        for link in links:
            print(f"  [{link['text']}] -> {link['href']}")

        print("Current URL:", page.url)
        await browser.close()

asyncio.run(find())
