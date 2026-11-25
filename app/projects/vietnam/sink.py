"""Vietnam statistics workbook and six-column worksheet contract."""
from decimal import Decimal

from app.sinks.qingtui_document import QingTuiDocumentSink


class VietnamQingTuiSink(QingTuiDocumentSink):
    def _local_result(self, business_date, summary):
        # ReviewService supplies percentage points. WPS percentage cells use a fraction.
        accuracy = format(Decimal(str(summary["accuracy"])) / 100, "f")
        values = {**summary, "accuracy": accuracy}
        for field in ("correct", "incorrect", "invalid"):
            if values[field] == 0:
                values[field] = ""
        return super()._local_result(business_date, values)

    def _select_sheet_if_configured(self, page, selectors):
        frame = self._find_wps_frame(page)
        if frame is None:
            raise RuntimeError("越南轻推文档尚未加载工作表。")
        spec = self._column_specs(self.config["sink"])
        headers = [column["header"] for column in spec]
        tabs = frame.locator(".sheet-name")
        matches = []
        for index in range(tabs.count()):
            tab = tabs.nth(index)
            tab.click()
            page.wait_for_timeout(300)
            table = self._parse_tsv_table(self._copy_used_range(page, selectors))
            if table and table[0][:len(headers)] == headers and all(not any(row[len(headers):]) for row in table):
                matches.append(tab)
        if len(matches) != 1:
            raise RuntimeError("未找到唯一匹配六列表头的越南工作表，请核对轻推文档。")
        matches[0].click()
        page.wait_for_timeout(300)

    def _build_expected_table(self, current_table, new_row, business_date, sink_cfg):
        # The active tab or its headers may have changed since selection.
        headers = [column["header"] for column in self._column_specs(sink_cfg)]
        if (not current_table or current_table[0][:len(headers)] != headers
                or any(any(row[len(headers):]) for row in current_table)):
            raise RuntimeError("越南工作表表头不匹配，或六列之外存在数据，未写入。")
        return super()._build_expected_table(current_table, new_row, business_date, sink_cfg)
