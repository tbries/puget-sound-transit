import csv
import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_data


class LoadServiceIdsAfterTest(unittest.TestCase):
    def write_csv(self, directory, name, fieldnames, rows):
        with (Path(directory) / name).open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_applies_calendar_ranges_and_date_exceptions_after_cutoff(self):
        calendar_fields = [
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ]
        date_fields = ["service_id", "date", "exception_type"]
        with tempfile.TemporaryDirectory() as directory:
            self.write_csv(
                directory,
                "calendar.txt",
                calendar_fields,
                [
                    {
                        "service_id": "future",
                        "monday": "1",
                        "tuesday": "0",
                        "wednesday": "0",
                        "thursday": "0",
                        "friday": "0",
                        "saturday": "0",
                        "sunday": "0",
                        "start_date": "20260830",
                        "end_date": "20260907",
                    },
                    {
                        "service_id": "expired",
                        "monday": "1",
                        "tuesday": "1",
                        "wednesday": "1",
                        "thursday": "1",
                        "friday": "1",
                        "saturday": "1",
                        "sunday": "1",
                        "start_date": "20260801",
                        "end_date": "20260829",
                    },
                    {
                        "service_id": "removed",
                        "monday": "0",
                        "tuesday": "0",
                        "wednesday": "0",
                        "thursday": "0",
                        "friday": "0",
                        "saturday": "0",
                        "sunday": "1",
                        "start_date": "20260830",
                        "end_date": "20260830",
                    },
                ],
            )
            self.write_csv(
                directory,
                "calendar_dates.txt",
                date_fields,
                [
                    {
                        "service_id": "removed",
                        "date": "20260830",
                        "exception_type": "2",
                    },
                    {
                        "service_id": "added",
                        "date": "20260830",
                        "exception_type": "1",
                    },
                    {
                        "service_id": "cutoff",
                        "date": "20260829",
                        "exception_type": "1",
                    },
                ],
            )

            with mock.patch.object(build_data, "GTFS_DIR", directory):
                result = build_data.load_service_ids_after(
                    datetime.date(2026, 8, 29)
                )

        self.assertEqual(result, {"future", "added"})


if __name__ == "__main__":
    unittest.main()
