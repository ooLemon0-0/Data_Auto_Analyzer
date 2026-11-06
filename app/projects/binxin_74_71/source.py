from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.core.config import settings
from app.core.models import SourceItem
from app.sources.base import DataSource
from app.core.http import http_get
from app.core.cancellation import check_cancelled, cancel_pause


class Binxin7471HistorySource(DataSource):
    """EasyConnect -> 镔鑫 history -> 当天记录元数据 -> 延迟缓存图片。"""

    parallel_image_downloads = True

    def __init__(self, project_config: dict):
        super().__init__(project_config)
        self.source = project_config["source"]
        self.data_root = Path(settings.storage.data_root).resolve()

    # ------------------------------------------------------------------
    # EasyConnect / intranet readiness
    # ------------------------------------------------------------------
    def _probe(self, url: str) -> bool:
        try:
            response = http_get(url, timeout=3)
            return response.status_code < 500
        except Exception:
            return False

    def _easyconnect_command(self) -> list[str]:
        ec = self.source.get("easyconnect", {})
        command = ec.get("launch_command") or []
        if command:
            return [str(x) for x in command]

        if not ec.get("auto_detect", True) or os.name != "nt":
            return []

        candidates = ec.get("candidate_executables") or [
            r"C:\Program Files (x86)\Sangfor\SSL\SangforCSClient\SangforCSClient.exe",
            r"C:\Program Files\Sangfor\SSL\SangforCSClient\SangforCSClient.exe",
            r"C:\Program Files (x86)\Sangfor\AF\SSL\SangforCSClient\SangforCSClient.exe",
            r"C:\Program Files\Sangfor\AF\SSL\SangforCSClient\SangforCSClient.exe",
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return [candidate]
        return []

    def _ensure_easyconnect(self) -> None:
        check_cancelled()
        ec = self.source.get("easyconnect", {})
        if not ec.get("enabled", True):
            return

        url = ec.get("healthcheck_url") or self.source["history_url"]
        if self._probe(url):
            return

        check_cancelled()
        command = self._easyconnect_command()
        if command:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        timeout = int(ec.get("connect_timeout_seconds", 90))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe(url):
                return
            cancel_pause(2)

        command_hint = command[0] if command else "未找到 EasyConnect 可执行文件，请在 config 中填写 launch_command"
        raise RuntimeError(
            f"EasyConnect/内网未就绪：{url}；等待 {timeout}s 后仍无法访问。EasyConnect: {command_hint}"
        )

    def check_available(self) -> bool:
        try:
            self._ensure_easyconnect()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Binxin web login
    # ------------------------------------------------------------------
    def _credential(self, key: str) -> str:
        auth = self.source.get("auth", {})
        configured = str(auth.get(key) or "").strip()
        if configured:
            return configured
        reference = str(auth.get("credentials_from_project", "binxin_billet") or "").strip()
        if not reference:
            return ""
        try:
            inherited = settings.project(reference).source.get("auth", {})
        except KeyError as exc:
            raise RuntimeError(f"网站登录凭据引用的项目不存在: {reference}") from exc
        return str(inherited.get(key) or "").strip()

    def _is_login_page(self, page) -> bool:
        selectors = self.source.get("auth", {}).get("selectors", {})
        username_selector = selectors.get("username", 'input[placeholder="请输入用户名"]')
        try:
            username = page.locator(username_selector).first
            if username.count() and username.is_visible(timeout=800):
                return True
        except Exception:
            pass
        return "#/login" in page.url

    def _ensure_site_login(self, page) -> None:
        """Login to the Binxin web app when the persistent profile has no valid session."""
        auth = self.source.get("auth", {})
        settle_timeout_ms = int(auth.get("page_settle_timeout_ms", 10000))
        try:
            page.wait_for_function(
                "() => location.hash.includes('/login') || document.querySelector('.page-login') || document.querySelector('.history')",
                timeout=settle_timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass

        if not self._is_login_page(page):
            return

        username = self._credential("username")
        password = self._credential("password")
        if not username or not password:
            raise RuntimeError(
                "镔鑫网站需要登录，但 source.auth.username/password 未配置。"
                "请直接填写 config.json。"
            )

        selectors = auth.get("selectors", {})
        username_selector = selectors.get("username", 'input[placeholder="请输入用户名"]')
        password_selector = selectors.get("password", 'input[placeholder="请输入密码"]')
        login_button_selector = selectors.get("login_button", 'button:has-text("登录")')
        timeout_ms = int(auth.get("login_timeout_ms", 30000))

        username_input = page.locator(username_selector).first
        password_input = page.locator(password_selector).first
        login_button = page.locator(login_button_selector).first

        username_input.wait_for(state="visible", timeout=timeout_ms)
        password_input.wait_for(state="visible", timeout=timeout_ms)
        login_button.wait_for(state="visible", timeout=timeout_ms)
        username_input.fill(username)
        password_input.fill(password)
        login_button.click()

        try:
            page.wait_for_function(
                "() => !location.hash.includes('/login') && !document.querySelector('.page-login')",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            # Keep credentials out of the exception message/logs.
            raise RuntimeError("镔鑫网站登录失败或等待登录结果超时，请检查用户名/密码。") from exc

        # Login usually lands on the index page. Always navigate back to the requested history page.
        page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)

    # ------------------------------------------------------------------
    # Browser helpers
    # ------------------------------------------------------------------
    def _launch_context(self, playwright):
        browser_cfg = self.source.get("browser", {})
        user_data_dir = Path(browser_cfg.get("user_data_dir", "./runtime/browser/binxin")).resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "headless": bool(browser_cfg.get("headless", False)),
            "accept_downloads": True,
            "viewport": None,
        }
        executable_path = browser_cfg.get("executable_path")
        if executable_path:
            kwargs["executable_path"] = executable_path
        else:
            kwargs["channel"] = browser_cfg.get("channel", "chrome")
        if browser_cfg.get("ignore_no_sandbox_arg", False):
            kwargs["ignore_default_args"] = ["--no-sandbox"]

        return playwright.chromium.launch_persistent_context(str(user_data_dir), **kwargs)

    def _set_day_filter(self, page, business_date: date) -> tuple[str, str]:
        selectors = self.source.get("selectors", {})
        start_selector = selectors.get("start_date", 'input.el-range-input[placeholder="开始时间"]')
        end_selector = selectors.get("end_date", 'input.el-range-input[placeholder="结束时间"]')

        start_text = f"{business_date.isoformat()} 00:00:00"
        end_text = f"{business_date.isoformat()} 23:59:59"

        start = page.locator(start_selector).first
        end = page.locator(end_selector).first
        start.wait_for(state="visible", timeout=20000)
        end.wait_for(state="visible", timeout=20000)

        start.click()
        start.fill(start_text)
        end.click()
        end.fill(end_text)
        end.press("Enter")

        # This page has no standalone query button in the supplied DOM snapshot.
        # Keep an optional selector for future versions that may add one.
        search_button = selectors.get("search_button")
        if search_button:
            locator = page.locator(search_button)
            if locator.count() and locator.first.is_visible():
                locator.first.click()

        expected = f"{start_text} ~ {end_text}"
        try:
            page.wait_for_function(
                "expected => document.body && document.body.innerText.includes(expected)",
                arg=expected,
                timeout=int(self.source.get("export", {}).get("filter_timeout_ms", 30000)),
            )
        except PlaywrightTimeoutError:
            # Some builds do not print the whole range text while still applying the Vue model.
            page.wait_for_timeout(2500)

        return start_text, end_text

    def _download_data_export(self, page, business_date: date) -> Path | None:
        export_cfg = self.source.get("export", {})
        if not export_cfg.get("archive_data_export", True):
            return None

        selector = self.source.get("selectors", {}).get("export_button", 'button:has-text("数据导出")')
        button = page.locator(selector).first
        if not button.count() or not button.is_visible():
            if export_cfg.get("archive_required", False):
                raise RuntimeError("找不到“数据导出”按钮")
            return None

        download_root = Path(
            self.source.get("browser", {}).get("download_dir", "./runtime/downloads/binxin")
        ).resolve()
        download_dir = download_root / business_date.isoformat()
        download_dir.mkdir(parents=True, exist_ok=True)

        try:
            with page.expect_download(timeout=int(export_cfg.get("download_timeout_ms", 60000))) as info:
                button.click()
            download = info.value
            suggested = download.suggested_filename or f"binxin_history_{business_date.isoformat()}.dat"
            destination = download_dir / suggested
            download.save_as(destination)
            return destination
        except PlaywrightTimeoutError:
            if export_cfg.get("archive_required", False):
                raise
            return None

    # ------------------------------------------------------------------
    # DOM scraping: collect metadata for all pages, not all image bytes.
    # ------------------------------------------------------------------
    @staticmethod
    def _text(cell) -> str:
        try:
            return cell.inner_text().strip()
        except Exception:
            return ""

    def _row_to_item(self, page, row, page_no: int, row_no: int) -> SourceItem | None:
        cells = row.locator("td")
        if cells.count() < 9:
            return None

        timestamp = self._text(cells.nth(1))
        recognition_type = self._text(cells.nth(2))
        image = cells.nth(3).locator("img").first
        image_src = image.get_attribute("src") if image.count() else None
        recognition = self._text(cells.nth(4))
        true_value = self._text(cells.nth(5))
        manual_value = self._text(cells.nth(6))
        miss_reason = self._text(cells.nth(7))
        recognition_status = self._text(cells.nth(8))

        if not image_src:
            return None
        if image_src.startswith("data:"):
            image_url = image_src
            basename = "inline-image"
        else:
            image_url = urljoin(page.url, image_src)
            basename = Path(unquote(urlparse(image_url).path)).name

        key_material = f"{timestamp}|{basename}|{recognition}|{image_url}"
        digest = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:16]
        source_key = f"{timestamp.replace('/', '').replace(':', '').replace(' ', '_')}_{digest}"

        return SourceItem(
            source_key=source_key,
            recognition_text=recognition,
            image_url=image_url,
            metadata={
                "timestamp": timestamp,
                "recognition_type": recognition_type,
                "recognition": recognition,
                "true_value": true_value,
                "manual_review_value": manual_value,
                "unrecognized_reason": miss_reason,
                "recognition_status": recognition_status,
                "image_url": image_url,
                "image_name": basename,
                "source_page": page_no,
                "source_row": row_no,
            },
        )

    def _scrape_current_page(self, page, page_no: int) -> list[SourceItem]:
        row_selector = self.source.get("selectors", {}).get(
            "table_rows", ".table-wrapper .el-table__body tbody tr.el-table__row"
        )
        rows = page.locator(row_selector)
        rows.first.wait_for(state="visible", timeout=30000)
        result: list[SourceItem] = []
        for i in range(rows.count()):
            item = self._row_to_item(page, rows.nth(i), page_no, i + 1)
            if item:
                result.append(item)
        return result

    @staticmethod
    def _table_signature(page, row_selector: str) -> str:
        """Return a stable-enough marker used only to observe table replacement."""
        row = page.locator(row_selector).first
        if not row.count():
            return ""
        text = row.inner_text().strip()
        image = row.locator("img").first
        image_src = image.get_attribute("src") if image.count() else ""
        return f"{text}\n{image_src or ''}"

    @staticmethod
    def _exact_page_button(page, page_number: int):
        """Element Plus may render 1, 10 and 11 together; match exact text."""
        buttons = page.locator(".el-pagination .el-pager li.number")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if button.inner_text().strip() == str(page_number):
                return button
        return None

    def _scrape_all_pages(self, page) -> list[SourceItem]:
        pagination_cfg = self.source.get("pagination", {})
        next_selector = pagination_cfg.get("next_button", ".el-pagination button.btn-next")
        active_selector = pagination_cfg.get("active_page", ".el-pagination .el-pager li.is-active.number")
        row_selector = self.source.get("selectors", {}).get(
            "table_rows", ".table-wrapper .el-table__body tbody tr.el-table__row"
        )
        max_pages = int(pagination_cfg.get("max_pages", 200))
        page_change_timeout_ms = int(pagination_cfg.get("page_change_timeout_ms", 15000))

        # Ensure we are on page 1 after a new date filter.
        first_page = self._exact_page_button(page, 1)
        if first_page is not None and first_page.is_visible():
            active = page.locator(active_selector).first
            active_text = active.inner_text().strip() if active.count() else "1"
            if active_text != "1":
                before_signature = self._table_signature(page, row_selector)
                first_page.click()
                try:
                    page.wait_for_function(
                        """
                        ([activeSelector, rowSelector, beforeSignature]) => {
                          const active = document.querySelector(activeSelector);
                          const row = document.querySelector(rowSelector);
                          if (!active || active.textContent.trim() !== '1' || !row) return false;
                          const image = row.querySelector('img');
                          const signature = `${row.innerText.trim()}\n${image?.getAttribute('src') || ''}`;
                          return signature && signature !== beforeSignature;
                        }
                        """,
                        arg=[active_selector, row_selector, before_signature],
                        timeout=page_change_timeout_ms,
                    )
                except PlaywrightTimeoutError as exc:
                    raise RuntimeError("分页返回第 1 页后，表格数据未刷新") from exc

        all_items: list[SourceItem] = []
        seen_keys: set[str] = set()

        for _ in range(max_pages):
            active = page.locator(active_selector).first
            page_no = int(active.inner_text().strip()) if active.count() else 1
            page_items = self._scrape_current_page(page, page_no)
            for item in page_items:
                if item.source_key not in seen_keys:
                    seen_keys.add(item.source_key)
                    all_items.append(item)

            next_button = page.locator(next_selector).first
            if not next_button.count() or next_button.is_disabled():
                break

            before_page = page_no
            before_signature = self._table_signature(page, row_selector)
            next_button.click()
            try:
                page.wait_for_function(
                    """
                    ([activeSelector, rowSelector, beforePage, beforeSignature]) => {
                      const active = document.querySelector(activeSelector);
                      const row = document.querySelector(rowSelector);
                      if (!active || active.textContent.trim() === String(beforePage) || !row) return false;
                      const image = row.querySelector('img');
                      const signature = `${row.innerText.trim()}\n${image?.getAttribute('src') || ''}`;
                      return signature && signature !== beforeSignature;
                    }
                    """,
                    arg=[active_selector, row_selector, before_page, before_signature],
                    timeout=page_change_timeout_ms,
                )
            except PlaywrightTimeoutError as exc:
                active = page.locator(active_selector).first
                active_text = active.inner_text().strip() if active.count() else "未知"
                raise RuntimeError(
                    f"分页从第 {before_page} 页切换后表格数据未刷新（当前页码: {active_text}）"
                ) from exc
        else:
            raise RuntimeError(f"分页超过 max_pages={max_pages}，为避免死循环已停止")

        return all_items

    @staticmethod
    def _day_bounds_ms(business_date: date) -> tuple[int, int]:
        # Site timestamps and the business calendar use China Standard Time.
        china = timezone(timedelta(hours=8))
        start = datetime.combine(business_date, datetime_time.min, tzinfo=china)
        end = start + timedelta(days=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1

    def _api_record_to_item(
        self, record: dict, business_date: date, page_no: int, row_no: int
    ) -> SourceItem:
        lower, upper = self._day_bounds_ms(business_date)
        try:
            acc_time = int(record["accTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("历史接口记录缺少有效采集时间") from exc
        if not lower <= acc_time <= upper:
            raise RuntimeError("历史接口返回了目标日期之外的记录")
        # Keep the same review image and source key as the existing DOM source.
        image_url = str(record.get("identifyPictureUrl") or record.get("identifyFullUrl") or "").strip()
        if not image_url:
            raise RuntimeError("历史接口记录缺少识别图片")
        china = timezone(timedelta(hours=8))
        timestamp = datetime.fromtimestamp(acc_time / 1000, china).strftime("%Y/%m/%d %H:%M:%S")
        recognition = str(record.get("identifyBilletNumber") or "").strip()
        basename = Path(unquote(urlparse(image_url).path)).name
        key_material = f"{timestamp}|{basename}|{recognition}|{image_url}"
        digest = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:16]
        source_key = f"{timestamp.replace('/', '').replace(':', '').replace(' ', '_')}_{digest}"
        return SourceItem(
            source_key=source_key,
            recognition_text=recognition,
            image_url=image_url,
            metadata={
                "timestamp": timestamp,
                "station": self.source.get("station", "74"),
                "recognition_type": record.get("identifyTypeEnum") or "",
                "recognition": recognition,
                "true_value": record.get("identifyBilletNumberReal") or "",
                "manual_review_value": record.get("artificialCheckNumber") or "",
                "unrecognized_reason": record.get("reasonNotIdentify") or "",
                "recognition_status": record.get("identifyStatusEnum") or "",
                "image_url": image_url,
                "image_name": basename,
                "original_image_url": record.get("identifyFullUrl") or "",
                "record_id": record.get("id"),
                "source_page": page_no,
                "source_row": row_no,
            },
        )

    def _fetch_filtered_api_items(self, page, business_date: date):
        lower, upper = self._day_bounds_ms(business_date)
        api_cfg = self.source.get("api", {})
        timeout_ms = int(api_cfg.get("request_timeout_ms", 60000))
        page_size = int(api_cfg.get("page_size", 500))
        max_pages = int(self.source.get("pagination", {}).get("max_pages", 200))
        if page_size < 1:
            raise ValueError("source.api.page_size 必须大于 0")

        def wait_idle():
            page.wait_for_function(
                """() => {
                  const root = document.querySelector('.history');
                  return root && [...root.querySelectorAll('.el-loading-mask')].every(mask => {
                    const style = getComputedStyle(mask);
                    const rect = mask.getBoundingClientRect();
                    return style.display === 'none' || style.visibility === 'hidden'
                      || style.opacity === '0' || rect.width === 0 || rect.height === 0;
                  });
                }""",
                timeout=timeout_ms,
            )

        def matches(response):
            if "/v1/history/condition-query" not in response.url:
                return False
            try:
                body = json.loads(response.request.post_data or "{}")
                return body.get("lowerTime") == lower and body.get("upperTime") == upper - 999
            except (TypeError, ValueError, AttributeError):
                return False

        # Let the opening query settle before applying another time window.
        wait_idle()
        try:
            with page.expect_response(matches, timeout=timeout_ms) as captured:
                start_text, end_text = self._set_day_filter(page, business_date)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"{business_date.isoformat()} 时间筛选请求未生效或接口响应超时") from exc
        response = captured.value
        if not response.ok:
            raise RuntimeError(f"历史时间筛选失败: HTTP {response.status}")
        wait_idle()
        request = response.request
        parts = urlsplit(request.url)
        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        body = json.loads(request.post_data or "{}")
        now_ms = int(time.time() * 1000)
        body.update(lowerTime=lower, upperTime=min(upper, now_ms) if lower <= now_ms <= upper else upper)
        available = request.all_headers()
        # BrowserContext supplies the authenticated cookies without logging them.
        headers = {
            name: available[name]
            for name in ("accept", "content-type", "origin", "referer", "x-server-origin")
            if available.get(name)
        }
        items: list[SourceItem] = []
        seen: set[str] = set()
        page_no, total_pages, expected = 1, 1, None
        while page_no <= total_pages:
            result = page.context.request.post(
                endpoint,
                params={"currPage": page_no, "pageSize": page_size, "sort": "accTime,descending"},
                data=body,
                headers=headers,
                timeout=timeout_ms,
            )
            if not result.ok:
                raise RuntimeError(f"历史数据第 {page_no} 页拉取失败: HTTP {result.status}")
            envelope = result.json()
            payload = envelope.get("data") if isinstance(envelope, dict) else None
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise RuntimeError(f"历史数据第 {page_no} 页返回格式错误")
            count = payload.get("rowCount")
            if not isinstance(count, int) or count < 0:
                raise RuntimeError("历史接口缺少有效记录总数")
            if expected is None:
                expected = count
                total_pages = max(1, (expected + page_size - 1) // page_size)
                if total_pages > max_pages:
                    raise RuntimeError(f"API 需要 {total_pages} 页，超过 max_pages={max_pages}")
            elif count != expected:
                raise RuntimeError("历史数据总数在分页期间发生变化，请重新拉取")
            for row_no, record in enumerate(payload["data"], 1):
                if not isinstance(record, dict):
                    raise RuntimeError("历史接口记录格式错误")
                item = self._api_record_to_item(record, business_date, page_no, row_no)
                if item.source_key in seen:
                    raise RuntimeError(f"历史数据第 {page_no} 页包含重复记录")
                seen.add(item.source_key)
                items.append(item)
            page_no += 1
        if len(items) != expected:
            raise RuntimeError(f"历史数据拉取不完整: 接口报告 {expected} 条，实际有效 {len(items)} 条")
        return start_text, end_text, items

    def fetch_day(self, business_date: date) -> list[SourceItem]:
        from app.sources.billet_http import fetch_day
        return fetch_day(self, business_date)

    @staticmethod
    def _detect_image_type(data: bytes) -> str | None:
        """
        根据文件 magic bytes 判断图片类型。

        镔鑫 19000 文件服务会把 JPG 返回为：
            Content-Type: application/octet-stream

        因此这里不能依赖 Content-Type，而是检查文件真实字节。
        """
        if not data:
            return None

        # JPEG: FF D8 FF
        if data.startswith(b"\xff\xd8\xff"):
            return "jpg"

        # PNG
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"

        # GIF
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "gif"

        # BMP
        if data.startswith(b"BM"):
            return "bmp"

        # WEBP
        if (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        ):
            return "webp"

        return None

    def _save_image_bytes(
        self,
        data: bytes,
        content_type: str,
        destination: Path,
        tmp: Path,
    ) -> Path:
        """校验真实图片字节后原子写入本地缓存。"""
        image_type = self._detect_image_type(data)
        if image_type is None:
            head = data[:32]
            raise RuntimeError(
                "response is not a valid image: "
                f"content-type={content_type or '<empty>'}, "
                f"size={len(data)} bytes, "
                f"head={head!r}"
            )

        check_cancelled()
        tmp.write_bytes(data)
        check_cancelled()
        tmp.replace(destination)
        return destination

    # ------------------------------------------------------------------
    # Lazy image cache: only sampled/reviewed images are downloaded.
    # ------------------------------------------------------------------
    def materialize_image(self, image_url: str, destination: Path) -> Path:
        from app.sources.billet_http import materialize_image
        return materialize_image(self, image_url, destination)
