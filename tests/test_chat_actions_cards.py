import unittest

# Importing is itself part of the test: nothing else in the suite imports
# chat_actions, so a syntax error there would otherwise only show up when
# the chat UI starts.
import chat_actions


class ApprovalCardIdTests(unittest.TestCase):
    def test_reversible_and_irreversible_cards_get_distinct_prefixes(self):
        reversible = chat_actions._card_ids({"reversible": True}, "abc")
        irreversible = chat_actions._card_ids({"reversible": False}, "abc")

        self.assertEqual(reversible["approve_remediation"], "approve-abc")
        self.assertEqual(irreversible["approve_remediation"], "approve-irreversible-abc")
        self.assertEqual(reversible["reject_remediation"], "reject-abc")


if __name__ == "__main__":
    unittest.main()
