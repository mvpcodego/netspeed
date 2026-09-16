#!/usr/bin/env python3
"""Тесты расчётной части — без сети.

Замер зависит от сети, но арифметика и проверки согласованности — нет.
Они вынесены в чистые функции, поэтому их можно проверить.
"""

import unittest

from netspeed import Attempt, build_request, check_consistency, summarize


def ok_attempt(bytes_read=5_000_000, connect_s=0.05, transfer_s=1.0, content_length=None):
    return Attempt(
        ok=True,
        status=200,
        bytes_read=bytes_read,
        connect_s=connect_s,
        transfer_s=transfer_s,
        total_s=connect_s + transfer_s,
        content_length=content_length,
    )


class TestSpeedMath(unittest.TestCase):
    def test_transfer_speed(self):
        # 1 МБ (1_000_000 байт) за 1 секунду = 8 Мбит/с
        a = ok_attempt(bytes_read=1_000_000, connect_s=0.0, transfer_s=1.0)
        self.assertAlmostEqual(a.transfer_mbit_s, 8.0, places=6)

    def test_effective_speed_lower_than_transfer(self):
        """Скорость с учётом соединения всегда ниже чистой передачи."""
        a = ok_attempt(bytes_read=1_000_000, connect_s=1.0, transfer_s=1.0)
        self.assertAlmostEqual(a.transfer_mbit_s, 8.0, places=6)
        self.assertAlmostEqual(a.effective_mbit_s, 4.0, places=6)
        self.assertLess(a.effective_mbit_s, a.transfer_mbit_s)

    def test_failed_attempt_has_zero_speed(self):
        a = Attempt(ok=False, error="таймаут")
        self.assertEqual(a.transfer_mbit_s, 0.0)
        self.assertEqual(a.effective_mbit_s, 0.0)

    def test_zero_transfer_time_does_not_divide_by_zero(self):
        a = ok_attempt(transfer_s=0.0)
        self.assertEqual(a.transfer_mbit_s, 0.0)


class TestSummarize(unittest.TestCase):
    def test_counts_and_totals(self):
        attempts = [ok_attempt(), ok_attempt(), Attempt(ok=False, error="сеть")]
        s = summarize(attempts)
        self.assertEqual(s["ok_count"], 2)
        self.assertEqual(s["failed_count"], 1)
        self.assertEqual(s["total_bytes"], 10_000_000)

    def test_median_resists_outlier(self):
        """Одна просадка сдвигает среднее, но не медиану — поэтому в отчёте оба."""
        attempts = [ok_attempt(transfer_s=1.0) for _ in range(9)]
        attempts.append(ok_attempt(transfer_s=10.0))  # просадка канала
        s = summarize(attempts)
        self.assertLess(s["avg_transfer_mbit_s"], s["median_transfer_mbit_s"])

    def test_all_failed(self):
        s = summarize([Attempt(ok=False, error="сеть") for _ in range(3)])
        self.assertEqual(s["ok_count"], 0)
        self.assertEqual(s["failed_count"], 3)


class TestConsistencyChecks(unittest.TestCase):
    def test_content_length_mismatch_is_reported(self):
        a = ok_attempt(bytes_read=4_000_000, content_length=5_000_000)
        warnings = check_consistency([a])
        self.assertTrue(any("Content-Length" in w for w in warnings))

    def test_changing_size_is_reported(self):
        warnings = check_consistency([ok_attempt(bytes_read=5_000_000),
                                      ok_attempt(bytes_read=4_000_000)])
        self.assertTrue(any("менялся" in w for w in warnings))

    def test_small_file_is_reported(self):
        warnings = check_consistency([ok_attempt(bytes_read=50_000)])
        self.assertTrue(any("маленький" in w for w in warnings))

    def test_wide_spread_is_reported(self):
        """Разброс в разы обычно означает кэш или нестабильный канал."""
        warnings = check_consistency([ok_attempt(transfer_s=0.1), ok_attempt(transfer_s=5.0)])
        self.assertTrue(any("разброс" in w for w in warnings))

    def test_clean_run_has_no_warnings(self):
        attempts = [ok_attempt(content_length=5_000_000) for _ in range(3)]
        self.assertEqual(check_consistency(attempts), [])


class TestRequestBuilding(unittest.TestCase):
    def test_cache_busting_adds_unique_param(self):
        r1 = build_request("https://example.com/f.bin", bust_cache=True, keep_alive=True)
        r2 = build_request("https://example.com/f.bin", bust_cache=True, keep_alive=True)
        self.assertIn("_cb=", r1.full_url)
        self.assertNotEqual(r1.full_url, r2.full_url)

    def test_cache_busting_respects_existing_query(self):
        r = build_request("https://example.com/f?bytes=5", bust_cache=True, keep_alive=True)
        self.assertIn("?bytes=5&_cb=", r.full_url)

    def test_no_cache_busting_keeps_url_intact(self):
        url = "https://example.com/f.bin"
        r = build_request(url, bust_cache=False, keep_alive=True)
        self.assertEqual(r.full_url, url)

    def test_compression_disabled(self):
        """С gzip прочитанный объём не равен размеру файла — замер соврёт."""
        r = build_request("https://example.com/f.bin", bust_cache=False, keep_alive=True)
        self.assertEqual(r.get_header("Accept-encoding"), "identity")


if __name__ == "__main__":
    unittest.main(verbosity=2)
