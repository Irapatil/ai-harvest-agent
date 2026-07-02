"""
LinkedIn Lead Agent — discovers recruiter hiring posts on LinkedIn.

What this agent does
────────────────────
• Searches LinkedIn POSTS (NOT Jobs) for hiring keywords.
• Extracts post author (recruiter), headline, company, post URL.
• Extracts email / phone ONLY if explicitly shared in the post text.
• Navigates to the recruiter's LinkedIn profile to get more detail.

Security contract (identical to all other agents in this repo)
───────────────────────────────────────────────────────────────
• Uses the persistent Chrome profile at data/chrome_profile/.
• User is already logged in to LinkedIn manually.
• DO NOT implement or call any login / OTP / MFA flow.
• DO NOT fabricate any email or phone number.
• email_status / phone_status: PUBLIC (post-shared) or NOT_FOUND only.
• Contact is only stored if scraped verbatim from a public post.

Page flow
─────────
1. Navigate to LinkedIn content search with the given keyword.
2. Dismiss any overlay / consent / modal.
3. Scroll to load more posts.
4. For each post, extract author + post metadata using JS evaluation.
5. Regex-scan post text for any explicitly shared email or phone.
6. Optionally visit recruiter's profile for headline / company / location.
7. Return list[LinkedInPost].
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

import structlog

from app.models.lead_models import LinkedInPost

logger = structlog.get_logger(__name__)

# ── Regex patterns for contact extraction from post text ─────────────────────
# Only matches email/phone that the recruiter explicitly typed in their post.
_EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)
_PHONE_RE = re.compile(
    r'(?:'
    r'(?:\+91|0091|91)?[\s\-]?[6-9]\d{9}'   # India mobile (10 digits, starts 6-9)
    r'|(?:\+[1-9]\d{6,14})'                   # International E.164
    r')'
)

# Hiring-intent keywords used to filter posts that are actually job posts
_HIRING_KEYWORDS = {
    "hiring", "we are hiring", "we're hiring", "looking for",
    "open position", "open role", "job opening", "vacancy",
    "join our team", "join us", "career opportunity", "immediate joiner",
    "urgently hiring", "talent acquisition", "recruitment",
}


def _is_hiring_post(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _HIRING_KEYWORDS)


def _extract_email_from_text(text: str) -> str:
    """Return the first email found in post text, or empty string."""
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else ""


def _extract_phone_from_text(text: str) -> str:
    """Return the first phone found in post text, or empty string."""
    m = _PHONE_RE.search(text)
    if m:
        raw = m.group(0).strip()
        # Normalise whitespace / hyphens
        return re.sub(r'[\s\-]+', '', raw)
    return ""


def _parse_company_from_headline(headline: str) -> tuple[str, str]:
    """
    Parse 'Designation at Company' or 'Designation | Company' from a LinkedIn headline.
    Returns (designation, company).
    """
    for sep in (" at ", " @ ", " | ", " - "):
        if sep in headline:
            parts = headline.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return headline.strip(), ""


def _normalize_linkedin_url(url: str) -> str:
    """Strip query params from LinkedIn profile URLs for stable dedup keys."""
    if not url:
        return ""
    try:
        p = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse(p._replace(query="", fragment="")).rstrip("/")
    except Exception:
        return url


# ══════════════════════════════════════════════════════════════════════════════
# LinkedIn Lead Agent
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInLeadAgent:
    """
    Searches LinkedIn POST results for recruiter hiring activity.

    Accepts a Playwright page that is already navigated to the correct
    LinkedIn session (persistent Chrome profile). Does NOT open a new browser.
    """

    def __init__(
        self,
        max_posts:    int = 50,
        max_pages:    int = 5,
        scroll_times: int = 8,
    ) -> None:
        self._max_posts    = max_posts
        self._max_pages    = max_pages
        self._scroll_times = scroll_times

    # ── Public entry point ─────────────────────────────────────────────────────

    async def search_posts(
        self,
        page:    Any,
        keyword: str,
    ) -> list[LinkedInPost]:
        """
        Search LinkedIn POSTS for `keyword`, extract recruiter data.
        Returns list[LinkedInPost].
        """
        logger.info("linkedin_search_started", keyword=keyword, max_posts=self._max_posts)

        all_posts:    list[LinkedInPost] = []
        seen_profiles: set[str]          = set()

        for page_num in range(self._max_pages):
            url = self._build_search_url(keyword, page_num)
            logger.info("linkedin_page_navigate", page_num=page_num, url=url)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3_000)
                await self._dismiss_overlays(page)
                await self._scroll_to_load(page)
            except Exception as exc:
                logger.warning("linkedin_page_load_failed", page_num=page_num, error=str(exc))
                break

            raw_posts = await self._extract_posts_from_page(page)
            logger.info("linkedin_page_extracted", page_num=page_num, count=len(raw_posts))

            for raw in raw_posts:
                if len(all_posts) >= self._max_posts:
                    break

                profile_url = _normalize_linkedin_url(raw.get("author_profile_url", ""))
                if not raw.get("author_name") or not profile_url:
                    continue
                if profile_url in seen_profiles:
                    continue
                seen_profiles.add(profile_url)

                post_text = raw.get("post_content", "")
                if not _is_hiring_post(post_text):
                    continue

                headline   = raw.get("author_headline", "")
                designation, company = _parse_company_from_headline(headline)

                post = LinkedInPost(
                    post_url           = raw.get("post_url", ""),
                    author_name        = raw.get("author_name", "").strip(),
                    author_profile_url = profile_url,
                    author_headline    = headline,
                    author_company     = company or raw.get("author_company", ""),
                    post_content       = post_text,
                    post_date          = raw.get("post_date", ""),
                    raw_email          = _extract_email_from_text(post_text),
                    raw_phone          = _extract_phone_from_text(post_text),
                )

                logger.info(
                    "linkedin_post_found",
                    author        = post.author_name,
                    company       = post.author_company,
                    has_email     = bool(post.raw_email),
                    has_phone     = bool(post.raw_phone),
                    profile_url   = profile_url,
                )
                all_posts.append(post)

            if len(all_posts) >= self._max_posts:
                break

        logger.info(
            "linkedin_search_completed",
            keyword         = keyword,
            posts_found     = len(all_posts),
            with_email      = sum(1 for p in all_posts if p.raw_email),
            with_phone      = sum(1 for p in all_posts if p.raw_phone),
        )
        return all_posts

    # ── Profile enrichment (optional second pass) ───────────────────────────────

    async def enrich_profile(self, page: Any, post: LinkedInPost) -> LinkedInPost:
        """
        Visit the recruiter's LinkedIn profile page to get fuller details.
        Updates post.author_company and post.author_headline in-place.
        Does NOT try to extract contact info — LinkedIn hides it behind auth.
        """
        if not post.author_profile_url:
            return post
        try:
            await page.goto(
                post.author_profile_url, wait_until="domcontentloaded", timeout=20_000
            )
            await page.wait_for_timeout(2_500)

            data = await page.evaluate("""() => {
                const headline = document.querySelector(
                    '.text-body-medium.break-words, .pv-text-details__left-panel .text-body-medium'
                )?.textContent?.trim() || '';

                const location = document.querySelector(
                    '.text-body-small.inline.t-black--light.break-words'
                )?.textContent?.trim() || '';

                const company = document.querySelector(
                    '[data-field="experience_company_logo"] .t-bold span[aria-hidden="true"]'
                )?.textContent?.trim() || '';

                return { headline, location, company };
            }""")

            if data.get("headline") and not post.author_headline:
                post.author_headline = data["headline"]
                designation, company = _parse_company_from_headline(data["headline"])
                if company and not post.author_company:
                    post.author_company = company

        except Exception as exc:
            logger.debug(
                "linkedin_profile_enrich_skipped",
                url=post.author_profile_url,
                reason=str(exc),
            )
        return post

    # ── Internals ─────────────────────────────────────────────────────────────

    def _build_search_url(self, keyword: str, page_num: int) -> str:
        """Build LinkedIn content search URL for posts."""
        params = {
            "keywords": keyword,
            "origin":   "GLOBAL_SEARCH_HEADER",
            "sid":      "abc",
        }
        if page_num > 0:
            params["start"] = str(page_num * 10)
        base = "https://www.linkedin.com/search/results/content/"
        return base + "?" + urllib.parse.urlencode(params)

    async def _dismiss_overlays(self, page: Any) -> None:
        """Dismiss any modal / cookie / consent overlays."""
        dismiss_selectors = [
            "button[data-control-name='overlay.dismiss_accept_policy']",
            "button[aria-label='Dismiss']",
            "button.msg-overlay-bubble-header__control--close",
            "#artdeco-modal-outlet button[data-tracking-control-name='overlay.dismiss']",
        ]
        for sel in dismiss_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

    async def _scroll_to_load(self, page: Any) -> None:
        """Scroll down to trigger lazy-loaded posts."""
        for _ in range(self._scroll_times):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
                await page.wait_for_timeout(800)
            except Exception:
                break

    async def _extract_posts_from_page(self, page: Any) -> list[dict]:
        """
        JavaScript evaluation to extract post data from the LinkedIn search results page.

        Uses a layered selector strategy to handle LinkedIn's frequently-changing DOM.
        Returns a list of raw dicts (pre-model).
        """
        try:
            return await page.evaluate("""() => {
                const posts = [];

                // LinkedIn search results render posts inside <li> elements
                // Multiple selector variants for robustness across LinkedIn DOM versions
                const containers = Array.from(document.querySelectorAll(
                    'li.reusable-search__result-container, ' +
                    'div.feed-shared-update-v2, ' +
                    'div[data-urn], ' +
                    '.occludable-update'
                ));

                for (const el of containers) {
                    try {
                        // Author name — multiple selector strategies
                        const nameSelectors = [
                            '.feed-shared-actor__name',
                            '.update-components-actor__name',
                            '.app-aware-link .artdeco-entity-lockup__title',
                            '[data-anonymize="person-name"]',
                            '.entity-result__title-text a span[aria-hidden="true"]',
                        ];
                        let authorName = '';
                        for (const s of nameSelectors) {
                            const n = el.querySelector(s);
                            if (n && n.textContent.trim()) {
                                authorName = n.textContent.trim();
                                break;
                            }
                        }

                        // Author profile URL — find any /in/ link
                        const profileLinkEl = el.querySelector(
                            'a[href*="/in/"], a.feed-shared-actor__container-link, a.update-components-actor__container-link'
                        );
                        const profileUrl = profileLinkEl
                            ? (profileLinkEl.href || profileLinkEl.getAttribute('href') || '')
                            : '';

                        // Author headline / subtext
                        const headlineSelectors = [
                            '.feed-shared-actor__sub-description',
                            '.update-components-actor__description',
                            '.artdeco-entity-lockup__subtitle',
                        ];
                        let headline = '';
                        for (const s of headlineSelectors) {
                            const h = el.querySelector(s);
                            if (h && h.textContent.trim()) {
                                headline = h.textContent.trim();
                                break;
                            }
                        }

                        // Post content
                        const contentSelectors = [
                            '.feed-shared-update-v2__description',
                            '.feed-shared-text-view',
                            '.update-components-text',
                            '.attributed-text-segment-list__content',
                            '.commentary span[dir]',
                        ];
                        let content = '';
                        for (const s of contentSelectors) {
                            const c = el.querySelector(s);
                            if (c && c.textContent.trim()) {
                                content = c.textContent.trim();
                                break;
                            }
                        }

                        // Post URL — prefer /posts/ or /feed/update/ links
                        let postUrl = '';
                        const postLinkEl = el.querySelector(
                            'a[href*="/posts/"], a[href*="/feed/update/"], a.feed-shared-meta__link'
                        );
                        if (postLinkEl) {
                            postUrl = postLinkEl.href || postLinkEl.getAttribute('href') || '';
                        }

                        // Post date (relative, e.g. "2d", "1w")
                        const dateEl = el.querySelector(
                            '.feed-shared-actor__sub-description time, ' +
                            '.update-components-actor__sub-description time, ' +
                            'span.feed-shared-meta__item'
                        );
                        const postDate = dateEl ? dateEl.textContent.trim() : '';

                        if (authorName || profileUrl) {
                            posts.push({
                                author_name: authorName,
                                author_profile_url: profileUrl,
                                author_headline: headline,
                                author_company: '',   // parsed from headline in Python
                                post_content: content,
                                post_url: postUrl,
                                post_date: postDate,
                            });
                        }
                    } catch (e) {
                        // Skip malformed post
                    }
                }
                return posts;
            }""")
        except Exception as exc:
            logger.warning("linkedin_page_js_extraction_failed", error=str(exc))
            return []
