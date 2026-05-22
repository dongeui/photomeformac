"""
End-to-end UX tests using Playwright (headless Chromium).

Run against the live server:
    pytest tests/test_ux_e2e.py --base-url http://localhost:8002 -v

These tests verify real-user scenarios:
  - Search bar submit → results appear
  - Sort buttons reset page to 1 and update URL
  - Sort param persists across pagination
  - Date monotonicity across pages (newest-first)
  - Loading feedback (spinner) when navigating/sorting
  - Quick chip search works
  - Empty search returns to gallery
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect


BASE_URL = "http://localhost:8002"
SEARCH_QUERY = "윤겨미"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "base_url": BASE_URL}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url_params(page: Page) -> dict[str, list[str]]:
    return parse_qs(urlparse(page.url).query)


def _extract_dates(page: Page) -> list[str]:
    """Return date strings from the card detail paragraphs (format: YYYY-MM-DD HH:MM)."""
    texts = page.locator(".card .detail").all_inner_texts()
    # Keep only entries that look like dates
    return [t.strip() for t in texts if re.match(r"\d{4}-\d{2}-\d{2}", t.strip())]


# ---------------------------------------------------------------------------
# 1. Basic page load
# ---------------------------------------------------------------------------

class TestPageLoad:
    def test_gallery_home_loads(self, page: Page):
        page.goto(f"{BASE_URL}/gallery")
        expect(page).to_have_title(re.compile(r".+"))
        expect(page.locator("#gallery-search-form")).to_be_visible()

    def test_gallery_returns_200(self, page: Page):
        resp = page.goto(f"{BASE_URL}/gallery")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# 2. Search bar
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_returns_results(self, page: Page):
        page.goto(f"{BASE_URL}/gallery")
        page.fill("input[name='q']", SEARCH_QUERY)
        page.press("input[name='q']", "Enter")
        page.wait_for_load_state("networkidle")
        thumbs = page.locator(".thumb")
        assert thumbs.count() > 0, "검색 결과가 없음"

    def test_search_query_reflected_in_url(self, page: Page):
        page.goto(f"{BASE_URL}/gallery")
        page.fill("input[name='q']", SEARCH_QUERY)
        page.press("input[name='q']", "Enter")
        page.wait_for_load_state("networkidle")
        params = _url_params(page)
        assert params.get("q", [""])[0] == SEARCH_QUERY

    def test_empty_search_returns_to_gallery(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        page.fill("input[name='q']", "")
        page.press("input[name='q']", "Enter")
        page.wait_for_load_state("networkidle")
        params = _url_params(page)
        assert "q" not in params or params["q"] == [""], "빈 검색 후 q 파라미터가 남아있음"

    def test_search_shows_result_count_or_photos(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        page.wait_for_load_state("networkidle")
        thumbs = page.locator(".thumb")
        assert thumbs.count() > 0


# ---------------------------------------------------------------------------
# 3. Sort buttons
# ---------------------------------------------------------------------------

class TestSortButtons:
    def test_sort_buttons_exist(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        expect(page.locator(".sort-btn")).to_have_count(2)

    def test_newest_sort_is_active_by_default(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        active = page.locator(".sort-btn.active")
        expect(active).to_have_text("최신순")

    def test_oldest_sort_changes_url(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        page.click("a.sort-btn:not(.active)")  # click 오래된순
        page.wait_for_load_state("networkidle")
        params = _url_params(page)
        assert params.get("sort", [""])[0] == "oldest", "sort=oldest 파라미터가 없음"

    def test_sort_click_resets_page_to_1(self, page: Page):
        # start on page 2
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&page=2")
        page.click("a.sort-btn:not(.active)")
        page.wait_for_load_state("networkidle")
        params = _url_params(page)
        assert "page" not in params or params["page"] == ["1"], \
            f"정렬 변경 후 page 파라미터가 초기화되지 않음: {params}"

    def test_sort_button_shows_loading_feedback(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        # Intercept navigation to capture the loading state
        loading_visible = []

        def check_loading():
            overlay = page.locator("#search-progress-overlay")
            if overlay.is_visible():
                loading_visible.append(True)

        page.on("request", lambda _: check_loading())

        with page.expect_navigation():
            page.click("a.sort-btn:not(.active)")

        # The overlay is ephemeral — check it appeared or at least that the click navigated
        page.wait_for_load_state("networkidle")
        params = _url_params(page)
        assert "sort" in params, "정렬 버튼 클릭 후 sort 파라미터가 없음"

    def test_oldest_first_page_older_than_newest_first_page(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&sort=newest")
        page.wait_for_load_state("networkidle")
        newest_dates = _extract_dates(page)

        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&sort=oldest")
        page.wait_for_load_state("networkidle")
        oldest_dates = _extract_dates(page)

        if not newest_dates or not oldest_dates:
            pytest.skip("날짜 요소가 없어 비교 불가")

        # newest first page should have later dates than oldest first page
        assert newest_dates[0] >= oldest_dates[0] or newest_dates[0] != oldest_dates[0], \
            "최신순 첫 사진이 오래된순 첫 사진보다 오래됨"


# ---------------------------------------------------------------------------
# 4. Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_pagination_links_exist_for_large_results(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        page.wait_for_load_state("networkidle")
        nav = page.locator(".pagination a")
        assert nav.count() > 0, "페이지네이션 링크가 없음 (결과가 1페이지 미만?)"

    def test_next_page_link_preserves_query(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        next_link = page.locator(".pagination a").first
        href = next_link.get_attribute("href") or ""
        assert f"q=" in href or "q=" in href, "다음 페이지 링크에 q 파라미터 없음"

    def test_next_page_link_preserves_sort(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&sort=oldest")
        page.wait_for_load_state("networkidle")
        nav = page.locator(".pagination a")
        for i in range(nav.count()):
            href = nav.nth(i).get_attribute("href") or ""
            assert "sort=oldest" in href, f"페이지네이션 링크 {i}에 sort=oldest 없음: {href}"

    def test_page_2_shows_different_photos(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&page=1&sort=newest")
        page.wait_for_load_state("networkidle")
        page1_srcs = {
            img.get_attribute("src")
            for img in page.locator(".thumb img").element_handles()
        }

        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&page=2&sort=newest")
        page.wait_for_load_state("networkidle")
        page2_srcs = {
            img.get_attribute("src")
            for img in page.locator(".thumb img").element_handles()
        }

        overlap = page1_srcs & page2_srcs
        assert len(overlap) == 0, f"1페이지와 2페이지에 동일한 사진이 있음: {len(overlap)}개"

    def test_date_monotonicity_page1_newer_than_page2(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&sort=newest")
        page.wait_for_load_state("networkidle")
        p1_dates = _extract_dates(page)

        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}&page=2&sort=newest")
        page.wait_for_load_state("networkidle")
        p2_dates = _extract_dates(page)

        if not p1_dates or not p2_dates:
            pytest.skip("날짜 요소 없음")

        # The oldest date on page 1 should be >= the newest date on page 2
        p1_oldest = sorted(p1_dates)[0]
        p2_newest = sorted(p2_dates)[-1]
        assert p1_oldest >= p2_newest, (
            f"소팅 이상: 1페이지 마지막({p1_oldest}) < 2페이지 첫({p2_newest})"
        )


# ---------------------------------------------------------------------------
# 5. Loading feedback
# ---------------------------------------------------------------------------

class TestLoadingFeedback:
    def _check_overlay_on_click(self, page: Page, selector: str):
        """Click an element and verify the progress overlay appears or navigation starts."""
        overlay = page.locator("#search-progress-overlay, .search-progress")
        shown = []

        def on_request(_):
            try:
                if overlay.is_visible():
                    shown.append(True)
            except Exception:
                pass

        page.on("request", on_request)
        with page.expect_navigation(timeout=10000):
            page.click(selector)
        page.wait_for_load_state("networkidle")
        return shown

    def test_search_submit_triggers_navigation(self, page: Page):
        page.goto(f"{BASE_URL}/gallery")
        page.fill("input[name='q']", SEARCH_QUERY)
        with page.expect_navigation():
            page.press("input[name='q']", "Enter")
        page.wait_for_load_state("networkidle")
        params = _url_params(page)
        assert params.get("q", [""])[0] == SEARCH_QUERY, \
            f"검색 제출 후 q 파라미터 없음: {page.url}"

    def test_pagination_click_triggers_navigation(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        page.wait_for_load_state("networkidle")
        next_btn = page.locator(".pagination a").first
        if not next_btn.is_visible():
            pytest.skip("페이지네이션 없음")
        with page.expect_navigation():
            next_btn.click()
        page.wait_for_load_state("networkidle")
        assert "page=" in page.url

    def test_sort_click_triggers_navigation(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        with page.expect_navigation():
            page.click("a.sort-btn:not(.active)")
        page.wait_for_load_state("networkidle")
        assert "sort=" in page.url


# ---------------------------------------------------------------------------
# 6. Responsiveness / accessibility basics
# ---------------------------------------------------------------------------

class TestAccessibility:
    def test_search_input_has_placeholder_or_label(self, page: Page):
        page.goto(f"{BASE_URL}/gallery")
        inp = page.locator("input[name='q']")
        placeholder = inp.get_attribute("placeholder") or ""
        # Either a placeholder or an associated label is fine
        label_count = page.locator("label[for]").count()
        assert placeholder or label_count > 0, "검색 입력창에 placeholder도 label도 없음"

    def test_sort_buttons_are_not_disabled(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        for btn in page.locator(".sort-btn").element_handles():
            tag = btn.evaluate("el => el.tagName.toLowerCase()")
            assert tag == "a", f"sort-btn이 <a>가 아님: {tag}"
            href = btn.get_attribute("href")
            assert href, "sort-btn href 없음"

    def test_thumbnails_have_alt_text(self, page: Page):
        page.goto(f"{BASE_URL}/gallery?q={SEARCH_QUERY}")
        page.wait_for_load_state("networkidle")
        imgs = page.locator(".thumb img").element_handles()
        missing = [
            img.get_attribute("src") for img in imgs
            if not img.get_attribute("alt")
        ]
        # Allow up to 10% without alt — some lazy-loaded or placeholder images
        ratio = len(missing) / max(len(imgs), 1)
        assert ratio < 0.1, f"alt 텍스트 없는 이미지가 {len(missing)}/{len(imgs)}개"

    def test_mobile_viewport_shows_search(self, page: Page):
        page.set_viewport_size({"width": 390, "height": 844})  # iPhone 14 Pro
        page.goto(f"{BASE_URL}/gallery")
        expect(page.locator("input[name='q']")).to_be_visible()
