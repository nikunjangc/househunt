"""Correctness tests for the arrive-by router, against hand-worked answers.

Reference timetable (from ``fixtures.py``):

* R1 rail leaves RIDGEWOOD at 06:00 + 30n (n=0..4); GLEN_ROCK +5, SECAUCUS +30,
  NY_PENN +45. So the 08:00 reaches NY Penn at 08:45.
* F1 feeder bus leaves MAYWOOD at 06:10 + 30n (n=0..3), reaching GLEN_ROCK +15.
* B1 express bus leaves FORT_LEE at 06:00 + 20n (n=0..8), reaching PABT +35.
* P1 PATH leaves HOB_PATH at 06:00 + 10n (n=0..19), reaching PATH_33 +12.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from househunt.commute.gtfs import Feed, build_network, parse_gtfs_time
from househunt.commute.raptor import arrive_by, build_footpaths, profile

H = 3600
M = 60


class RaptorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests.fixtures import write_feed

        cls._tmp = tempfile.mkdtemp()
        path = write_feed(os.path.join(cls._tmp, "fixture.zip"))
        cls.feed = Feed.from_zip(path, name="fx")
        cls.net = build_network([cls.feed], service_weekday=2)
        build_footpaths(cls.net, max_walk_m=500)

    def sid(self, name: str) -> int:
        return self.net.stop_index[f"fx:{name}"]

    # -- parsing ---------------------------------------------------------

    def test_parses_times_past_midnight(self):
        self.assertEqual(parse_gtfs_time("00:00:00"), 0)
        self.assertEqual(parse_gtfs_time("08:30:00"), 8 * H + 30 * M)
        # Trips running past midnight must not wrap to 01:10.
        self.assertEqual(parse_gtfs_time("25:10:00"), 25 * H + 10 * M)

    def test_network_groups_trips_into_patterns(self):
        # Five routes, each with exactly one stop sequence -> five patterns.
        self.assertEqual(len(self.net.patterns), 5)
        self.assertEqual(len(self.net.stops), 11)
        counts = {self.net.patterns[i].route_id: self.net.patterns[i].n_trips
                  for i in range(len(self.net.patterns))}
        self.assertEqual(counts["fx:R1"], 5)
        self.assertEqual(counts["fx:B1"], 9)

    def test_saturday_has_no_service(self):
        weekend = build_network([self.feed], service_weekday=5)
        self.assertEqual(len(weekend.patterns), 0)

    def test_footpath_links_hoboken_to_path(self):
        walkable = {other for other, _ in self.net.footpaths[self.sid("HOBOKEN")]}
        self.assertIn(self.sid("HOB_PATH"), walkable)
        # Ridgewood is 30km away; it must not be walkable to anything here.
        self.assertEqual(self.net.footpaths[self.sid("RIDGEWOOD")], [])

    # -- routing ---------------------------------------------------------

    def targets(self):
        return [(self.sid("NY_PENN"), 0), (self.sid("PABT"), 0), (self.sid("PATH_33"), 0)]

    def test_one_seat_rail_ride(self):
        labels = arrive_by(self.net, self.targets(), deadline=9 * H)
        ridgewood = labels[self.sid("RIDGEWOOD")]
        # Latest train reaching NY Penn by 09:00 is the 08:00 (arrives 08:45).
        self.assertEqual(ridgewood.departure, 8 * H)
        self.assertEqual(ridgewood.trips, 1, "rail to Penn is a one-seat ride")
        self.assertEqual(ridgewood.first_route, "fx:R1")

    def test_express_bus_is_a_one_seat_ride_to_pabt(self):
        labels = arrive_by(self.net, self.targets(), deadline=9 * H)
        fort_lee = labels[self.sid("FORT_LEE")]
        # Buses reach PABT at +35; latest arriving by 09:00 departs 08:20.
        self.assertEqual(fort_lee.departure, 8 * H + 20 * M)
        self.assertEqual(fort_lee.trips, 1)

    def test_transfer_counted_for_feeder_bus_then_rail(self):
        labels = arrive_by(self.net, self.targets(), deadline=9 * H)
        maywood = labels[self.sid("MAYWOOD")]
        # F1 07:40 -> GLEN_ROCK 07:55, then R1 passes GLEN_ROCK at 08:05
        # and reaches NY Penn 08:45. Two vehicles = one transfer.
        self.assertEqual(maywood.trips, 2)
        self.assertEqual(maywood.departure, 7 * H + 40 * M)

    def test_walk_then_ride_is_still_one_trip(self):
        labels = arrive_by(self.net, self.targets(), deadline=9 * H)
        hoboken = labels[self.sid("HOBOKEN")]
        # Hoboken's own bus goes nowhere useful, so the journey is
        # walk -> HOB_PATH -> PATH. Walking is not a vehicle trip.
        self.assertEqual(hoboken.trips, 1)
        self.assertEqual(hoboken.first_route, "fx:P1")
        # PATH takes 12 min, so the last departure landing by 09:00 is 08:40.
        walk = dict(self.net.footpaths[self.sid("HOBOKEN")])[self.sid("HOB_PATH")]
        self.assertEqual(hoboken.departure, 8 * H + 40 * M - walk)

    def test_max_rounds_limits_transfers(self):
        # With a single round allowed, Maywood (which needs two vehicles)
        # must drop out entirely while one-seat rides survive.
        labels = arrive_by(self.net, self.targets(), deadline=9 * H, max_rounds=1)
        self.assertNotIn(self.sid("MAYWOOD"), labels)
        self.assertIn(self.sid("RIDGEWOOD"), labels)

    def test_unreachable_stop_is_absent(self):
        # Nothing runs from PABT outbound, so it is a target, not an origin.
        labels = arrive_by(self.net, [(self.sid("NY_PENN"), 0)], deadline=9 * H)
        self.assertNotIn(self.sid("FORT_LEE"), labels)

    def test_deadline_before_service_yields_nothing(self):
        labels = arrive_by(self.net, self.targets(), deadline=5 * H)
        self.assertEqual(labels, {})

    def test_egress_time_shifts_the_deadline(self):
        # A 10-minute walk from the platform forces an earlier train: the
        # 08:00 arrives 08:45 and 08:45+10 > 08:50, so the 07:30 is the latest.
        labels = arrive_by(self.net, [(self.sid("NY_PENN"), 10 * M)], deadline=8 * H + 50 * M)
        self.assertEqual(labels[self.sid("RIDGEWOOD")].departure, 7 * H + 30 * M)

    # -- profiles --------------------------------------------------------

    def test_profile_recovers_true_travel_time(self):
        # Sweeping deadlines on a fine grid drives the end-slack out of the
        # measurement: Ridgewood -> NY Penn is genuinely a 45-minute ride.
        deadlines = range(8 * H, 9 * H + 1, 5 * M)
        profiles = profile(self.net, self.targets(), deadlines)
        self.assertEqual(profiles[self.sid("RIDGEWOOD")].best_minutes, 45)
        self.assertEqual(profiles[self.sid("FORT_LEE")].best_minutes, 35)

    def test_profile_captures_frequency_difference(self):
        deadlines = range(7 * H, 9 * H + 1, 5 * M)
        profiles = profile(self.net, self.targets(), deadlines)
        # PATH every 10 min beats rail every 30 min on usable departures,
        # even though both are one-seat rides.
        path_departures = len(profiles[self.sid("HOB_PATH")].departures)
        rail_departures = len(profiles[self.sid("RIDGEWOOD")].departures)
        self.assertGreater(path_departures, rail_departures)

    def test_profile_reports_transfers_and_one_seat_flag(self):
        deadlines = range(8 * H, 9 * H + 1, 5 * M)
        profiles = profile(self.net, self.targets(), deadlines)
        self.assertTrue(profiles[self.sid("RIDGEWOOD")].one_seat_ride)
        self.assertEqual(profiles[self.sid("RIDGEWOOD")].transfers, 0)
        self.assertFalse(profiles[self.sid("MAYWOOD")].one_seat_ride)
        self.assertEqual(profiles[self.sid("MAYWOOD")].transfers, 1)

    def test_travel_budget_prunes_far_stops(self):
        deadlines = range(8 * H, 9 * H + 1, 5 * M)
        tight = profile(self.net, self.targets(), deadlines, max_travel_seconds=40 * M)
        # Fort Lee is a 35-minute ride and survives; Ridgewood at 45 does not.
        self.assertIn(self.sid("FORT_LEE"), tight)
        self.assertNotIn(self.sid("RIDGEWOOD"), tight)


if __name__ == "__main__":
    unittest.main()
