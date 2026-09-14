import unittest

from backend.utils.asof import parse_as_of_ms


class AsOfParsingTests(unittest.TestCase):
    def test_none_and_empty_mean_realtime(self):
        self.assertIsNone(parse_as_of_ms(None))
        self.assertIsNone(parse_as_of_ms(""))
        self.assertIsNone(parse_as_of_ms("   "))

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_as_of_ms("not-a-date"))
        self.assertIsNone(parse_as_of_ms("2026-13-99"))

    def test_iso_z(self):
        ms = parse_as_of_ms("2026-09-01T00:00:00Z")
        self.assertEqual(ms, 1788220800000)

    def test_iso_with_timezone(self):
        self.assertEqual(
            parse_as_of_ms("2026-09-01T00:00:00+00:00"),
            parse_as_of_ms("2026-09-01T00:00:00Z"),
        )

    def test_naive_treated_as_utc(self):
        self.assertEqual(
            parse_as_of_ms("2026-09-01T00:00:00"),
            parse_as_of_ms("2026-09-01T00:00:00Z"),
        )

    def test_offset_equivalence(self):
        # 2026-09-01T02:00:00+02:00 == 2026-09-01T00:00:00Z
        self.assertEqual(
            parse_as_of_ms("2026-09-01T02:00:00+02:00"),
            parse_as_of_ms("2026-09-01T00:00:00Z"),
        )


if __name__ == "__main__":
    unittest.main()
