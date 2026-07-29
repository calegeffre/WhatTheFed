import unittest

from whatthefed import Document, KnowledgeGraphBuilder


class KnowledgeGraphBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = KnowledgeGraphBuilder(chunk_token_target=18, chunk_overlap_tokens=4)
        self.meeting_document = Document(
            source="fomc_2026_06",
            content=(
                "The Federal Open Market Committee approved the following statement for release by a 12-0 vote. "
                "The Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent. "
                "Inflation remains elevated and job gains have kept pace with the workforce."
            ),
            kind="meeting_note",
            meeting_date="2026-06-17",
            published_at="2026-06-17T14:00:00Z",
            source_url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260617a.htm",
        )
        self.market_document = Document(
            source="kalshi_sep_2026",
            content=(
                "Kalshi implied odds for the September 2026 FOMC meeting show a high hold probability with a smaller "
                "cut tail and limited raise risk."
            ),
            kind="kalshi_market",
            published_at="2026-07-28T00:00:00Z",
            source_url="https://kalshi.com/markets/fed/september-2026",
            metadata={
                "provider": "Kalshi",
                "market_name": "September 2026 FOMC target range",
                "target_meeting": "2026-09-16",
                "raise_probability": 0.12,
                "hold_probability": 0.73,
                "cut_probability": 0.15,
                "volume": 125000,
            },
        )

    def test_build_creates_chunks_and_topic_links(self) -> None:
        graph = self.builder.build([self.meeting_document])
        chunk_nodes = graph.nodes_by_kind("chunk")
        self.assertGreaterEqual(len(chunk_nodes), 2)
        inflation_neighbors = graph.neighbors(chunk_nodes[0].id, relation="mentions_topic")
        self.assertTrue(any(node.id == "topic:policy" for node in inflation_neighbors))
        self.assertTrue(any(edge.relation == "has_chunk" for edge in graph.edges))

    def test_build_extracts_meeting_vote_summary(self) -> None:
        graph = self.builder.build([self.meeting_document])
        vote_node = graph.nodes["vote:meeting:2026-06-17"]
        decision_node = graph.nodes["decision:meeting:2026-06-17"]
        self.assertEqual(vote_node.kind, "meeting_vote")
        self.assertEqual(vote_node.properties["official_tally"], "12-0")
        self.assertEqual(vote_node.properties["votes_for"], 12)
        self.assertEqual(decision_node.properties["decision"], "hold")

    def test_build_links_market_snapshot_to_target_meeting(self) -> None:
        graph = self.builder.build([self.meeting_document, self.market_document])
        snapshot_node = graph.nodes["snapshot:kalshi_sep_2026:2026-07-28T00:00:00Z"]
        linked_meetings = graph.neighbors(snapshot_node.id, relation="targets_meeting")
        self.assertEqual(snapshot_node.properties["hold_probability"], 0.73)
        self.assertEqual(len(linked_meetings), 1)
        self.assertEqual(linked_meetings[0].id, "meeting:2026-09-16")

    def test_graph_serializes_nodes_edges_and_chunks(self) -> None:
        graph = self.builder.build([self.meeting_document, self.market_document])
        payload = graph.to_dict()
        self.assertIn("nodes", payload)
        self.assertIn("edges", payload)
        self.assertIn("chunks", payload)
        self.assertGreaterEqual(len(payload["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
