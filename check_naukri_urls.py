import asyncio
import httpx

CANDIDATE_URLS = [
    "https://www.naukri.com/employer/login",
    "https://www.naukri.com/recruiter/login",
    "https://recruiter.naukri.com/",
    "https://recruiter.naukri.com/login",
    "https://www.naukri.com/ms/employer/login",
    "https://www.naukri.com/mnjuser/employerlogin",
    "https://www.naukri.com/employer-login/",
    "https://www.naukri.com/login?loginFor=recruiter",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

async def check():
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for url in CANDIDATE_URLS:
            try:
                r = await client.get(url, headers=HEADERS)
                print(f"{r.status_code}  {url}  -> final: {r.url}")
            except Exception as e:
                print(f"ERR  {url}  -> {e}")

asyncio.run(check())
