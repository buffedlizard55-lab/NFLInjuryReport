import unittest

from collectors.models import (
    is_escalation,
    norm_practice,
    norm_status,
    severity_rank,
    slugify,
)


class TestStatusNormalisation(unittest.TestCase):
    def test_official_wording(self):
        self.assertEqual(norm_status("Questionable"), "QUESTIONABLE")
        self.assertEqual(norm_status("Out"), "OUT")
        self.assertEqual(norm_status("Doubtful"), "DOUBTFUL")

    def test_rotowire_letters(self):
        self.assertEqual(norm_status("Q"), "QUESTIONABLE")
        self.assertEqual(norm_status("D"), "DOUBTFUL")
        self.assertEqual(norm_status("O"), "OUT")

    def test_roto_report_wording(self):
        self.assertEqual(norm_status("IR"), "IR")
        self.assertEqual(norm_status("IR-R"), "IR")
        self.assertEqual(norm_status("Reserve-Sus"), "SUSPENDED")
        self.assertEqual(norm_status("PUP-R"), "PUP")

    def test_unknown_is_never_guessed(self):
        # "Reserve-Ret" and "Reserve-CEL" are not availability designations;
        # inventing one would corrupt the scorecard.
        self.assertEqual(norm_status("Reserve-Ret"), "UNKNOWN")
        self.assertEqual(norm_status("Reserve-CEL"), "UNKNOWN")
        self.assertEqual(norm_status(""), "UNKNOWN")
        self.assertEqual(norm_status(None), "UNKNOWN")

    def test_practice(self):
        self.assertEqual(norm_practice("Full Participation in Practice"), "FULL")
        self.assertEqual(norm_practice("Did Not Participate In Practice"), "DNP")
        self.assertEqual(norm_practice("Limited Participation in Practice"), "LIMITED")
        self.assertEqual(norm_practice(""), "NONE")


class TestEscalation(unittest.TestCase):
    def test_out_is_worse_than_questionable(self):
        self.assertTrue(is_escalation("QUESTIONABLE", "OUT"))
        self.assertFalse(is_escalation("OUT", "QUESTIONABLE"))

    def test_ir_is_worse_than_out(self):
        self.assertTrue(severity_rank("IR") > severity_rank("OUT"))

    def test_unknown_status_change_is_still_a_change(self):
        self.assertTrue(is_escalation("UNKNOWN", "OUT"))


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("A.J. Brown"), "aj-brown")
        self.assertEqual(slugify("Jaxon Smith-Njigba"), "jaxon-smith-njigba")

    def test_suffixes_are_stripped_so_jr_matches(self):
        self.assertEqual(slugify("James Thompson Jr."), "james-thompson")
        self.assertEqual(slugify("James Thompson"), "james-thompson")

    def test_accents(self):
        self.assertEqual(slugify("José Núñez"), "jose-nunez")


if __name__ == "__main__":
    unittest.main()
