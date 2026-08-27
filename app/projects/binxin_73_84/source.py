from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import time
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.core.config import settings
from app.core.models import SourceItem
from app.sources.base import DataSource


class Binxin7384HistorySource(DataSource):
    """EasyConnect -> 镔鑫 history -> 当天记录元数据 -> 延迟缓存图片。"""

    def __init__(self, project_config: dict):
        super().__init__(project_config)
        self.source = project_config["source"]
        self.data_root = Path(settings.storage.data_root).resolve()

    # ------------------------------------------------------------------
    # EasyConnect / intranet readiness
    # ------------------------------------------------------------------
    def _probe(self, url: str) -> bool:
        try:
            response = requests.get(url, timeout=3)
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
        ec = self.source.get("easyconnect", {})
        if not ec.get("enabled", True):
            return

        url = ec.get("healthcheck_url") or self.source["history_url"]
        if self._probe(url):
            return

        command = self._easyconnect_command()
        if command:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        timeout = int(ec.get("connect_timeout_seconds", 90))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe(url):
                return
            time.sleep(2)

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
        return str(auth.get(key, "")).strip()

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

    def fetch_day(self, business_date: date) -> list[SourceItem]:
        self._ensure_easyconnect()
        with sync_playwright() as p:
            context = self._launch_context(p)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)
                self._ensure_site_login(page)
                page.locator(".history").wait_for(state="visible", timeout=30000)

                start_text, end_text = self._set_day_filter(page, business_date)
                page.wait_for_timeout(int(self.source.get("export", {}).get("wait_after_filter_ms", 1500)))
                archive = self._download_data_export(page, business_date)
                items = self._scrape_all_pages(page)

                for item in items:
                    item.metadata["filter_start"] = start_text
                    item.metadata["filter_end"] = end_text
                    item.metadata["data_export_archive"] = str(archive) if archive else ""

                if not items:
                    raise RuntimeError(f"{business_date.isoformat()} 没有抓到可审核记录")
                return items
            finally:
                context.close()

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

        tmp.write_bytes(data)
        tmp.replace(destination)
        return destination

    # ------------------------------------------------------------------
    # Lazy image cache: only sampled/reviewed images are downloaded.
    # ------------------------------------------------------------------
    def materialize_image(self, image_url: str, destination: Path) -> Path:
        """
        将远端图片缓存到本地。

        下载优先级：
        1. 普通 requests（镔鑫 19000 图片服务当前可直接访问）
        2. 如果失败，再使用已登录的 Chrome persistent context 请求

        注意：镔鑫图片服务把 JPEG 以 application/octet-stream 返回，
        所以两条链路都通过 magic bytes 判断图片，而不再要求 image/* MIME。
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".part")

        # 清理上次异常退出遗留的临时文件。
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

        # data:image/...;base64,...
        if image_url.startswith("data:"):
            try:
                header, encoded = image_url.split(",", 1)
                if ";base64" in header:
                    data = base64.b64decode(encoded)
                else:
                    data = unquote(encoded).encode("utf-8")

                content_type = (
                    header[5:].split(";", 1)[0].strip().lower()
                    if header.startswith("data:")
                    else ""
                )
                return self._save_image_bytes(
                    data=data,
                    content_type=content_type,
                    destination=destination,
                    tmp=tmp,
                )
            except Exception as exc:
                raise RuntimeError(f"内嵌图片解析失败: {exc}") from exc

        headers = {
            "Referer": self.source["history_url"],
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        request_error: Exception | None = None

        # --------------------------------------------------------------
        # 1. Direct requests
        # --------------------------------------------------------------
        try:
            response = requests.get(
                image_url,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()

            content_type = (response.headers.get("content-type") or "").lower()
            data = response.content

            return self._save_image_bytes(
                data=data,
                content_type=content_type,
                destination=destination,
                tmp=tmp,
            )
        except Exception as exc:
            request_error = exc
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

        # --------------------------------------------------------------
        # 2. Browser fallback
        # --------------------------------------------------------------
        with sync_playwright() as p:
            context = self._launch_context(p)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(
                    self.source["history_url"],
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                self._ensure_site_login(page)

                response = context.request.get(
                    image_url,
                    headers=headers,
                    timeout=30000,
                )
                if not response.ok:
                    raise RuntimeError(
                        f"HTTP {response.status}: {response.status_text}"
                    )

                content_type = (response.headers.get("content-type") or "").lower()
                data = response.body()

                return self._save_image_bytes(
                    data=data,
                    content_type=content_type,
                    destination=destination,
                    tmp=tmp,
                )
            except Exception as browser_error:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

                raise RuntimeError(
                    f"图片缓存失败: {image_url}; "
                    f"requests={request_error}; "
                    f"chrome={browser_error}"
                ) from browser_error
            finally:
                context.close()

