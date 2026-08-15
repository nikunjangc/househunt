"""ZIP scoring tests.

The fixture's stops sit at real North Jersey coordinates, so this exercises
the actual ZCTA polygons in ``data/nj_zips.geojson`` alongside the scoring
maths. The Manhattan targets fall outside NJ and must drop out.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from househunt.commute.geo import ZipIndex
from househunt.commute.gtfs import Feed, build_network
from househunt.commute.raptor import build_footpaths, profile
from househunt.commute.score import ScoringConfig, Weights, score_zips, to_csv

H = 3600
M = 60
GEOJSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "nj_zips.geojson")


@unittest.skipUnless(os.path.exists(GEOJSON), "NJ ZIP boundaries not downloaded")
class ScoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests.fixtures import write_feed

        cls._tmp = tempfile.mkdtemp()
        feed = Feed.from_zip(write_feed(os.path.join(cls._tmp, "fixture.zip")), name="fx")
        cls.net = build_network([feed], service_weekday=2)
        build_footpaths(cls.net, max_walk_m=500)
        cls.zips = ZipIndex.from_geojson(GEOJSON)

        targets = [
            (cls.net.stop_index["fx:NY_PENN"], 0),
            (cls.net.stop_index["fx:PABT"], 0),
            (cls.net.stop_index["fx:PATH_33"], 0),
        ]
        cls.profiles = profile(cls.net, targets, range(7 * H, 9 * H + 1, 5 * M))
        cls.scores = score_zips(
            cls.net,
            cls.profiles,
            cls.zips,
            ScoringConfig(budget_minutes=90),
        )
        cls.by_zip = {z.zip_code: z for z in cls.scores}

    def test_manhattan_targets_are_not_scored_as_nj_zips(self):
        # NY Penn / PABT / 33rd St are outside every NJ ZCTA.
        self.assertTrue(all(z.zip_code.startswith("0") for z in self.scores))

    def test_expected_towns_are_found(self):
        # Ridgewood 07450, Fort Lee 07024, Hoboken 07030.
        for zip_code in ("07450", "07024", "07030"):
            self.assertIn(zip_code, self.by_zip, f"{zip_code} missing from scores")

    def test_travel_times_match_the_timetable(self):
        self.assertEqual(self.by_zip["07450"].best_minutes, 45)  # rail
        self.assertEqual(self.by_zip["07024"].best_minutes, 35)  # express bus

    def test_one_seat_rides_flagged(self):
        self.assertTrue(self.by_zip["07450"].one_seat_ride)
        self.assertEqual(self.by_zip["07450"].transfers, 0)

    def test_express_service_detected_from_route_name(self):
        self.assertTrue(self.by_zip["07024"].has_express, "159X Fort Lee Express")
        self.assertFalse(self.by_zip["07450"].has_express)

    def test_modes_reported(self):
        self.assertEqual(self.by_zip["07450"].modes, ["rail"])
        self.assertEqual(self.by_zip["07024"].modes, ["bus"])

    def test_frequency_reflects_headway(self):
        # PATH every 10 min must beat rail every 30 min.
        self.assertGreater(
            self.by_zip["07030"].departures_per_hour,
            self.by_zip["07450"].departures_per_hour,
        )

    def test_transfer_penalised_against_one_seat_ride(self):
        # Maywood (07607) needs a feeder bus then rail.
        maywood = self.by_zip.get("07607")
        self.assertIsNotNone(maywood, "Maywood ZIP should be reachable at a 90-min budget")
        self.assertEqual(maywood.transfers, 1)
        self.assertLess(maywood.score, self.by_zip["07450"].score)

    def test_budget_excludes_slow_zips(self):
        tight = score_zips(self.net, self.profiles, self.zips, ScoringConfig(budget_minutes=40))
        codes = {z.zip_code for z in tight}
        self.assertIn("07024", codes)      # 35 min
        self.assertNotIn("07450", codes)   # 45 min

    def test_results_are_sorted_by_score(self):
        values = [z.score for z in self.scores]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_weights_change_the_ranking(self):
        # Weighting time alone should put the fastest ZIP on top regardless
        # of how good its frequency or route redundancy is.
        time_only = score_zips(
            self.net, self.profiles, self.zips,
            ScoringConfig(budget_minutes=90, weights=Weights(1, 0, 0, 0, 0)),
        )
        fastest = min(time_only, key=lambda z: z.best_minutes)
        self.assertEqual(time_only[0].zip_code, fastest.zip_code)

    def test_components_are_exposed_for_inspection(self):
        comps = self.by_zip["07450"].components
        self.assertEqual(
            set(comps), {"travel_time", "transfers", "frequency", "redundancy", "express"}
        )
        self.assertTrue(all(0 <= v <= 100 for v in comps.values()))

    def test_csv_round_trips(self):
        text = to_csv(self.scores)
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), len(self.scores) + 1)
        self.assertTrue(lines[0].startswith("zip_code,score,"))


if __name__ == "__main__":
    unittest.main()
