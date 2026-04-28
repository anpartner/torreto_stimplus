from __future__ import annotations

import unittest
from pathlib import Path

from app.core.settings import AkeneoSettings, CohereSettings, GroqSettings, Settings, TypesenseSettings
from app.domain.models import Product, SearchHit, StructuredQuery
from app.services.catalog_ingestion import AkeneoCatalogNormalizer
from app.services.akeneo_client import AttributeMetadata
from app.services.hybrid_search import HybridSearchService, RerankerService
from app.services.query_understanding import QueryUnderstandingService
from app.services.search_engine import build_search_application
from app.services.typesense_service import TypesenseService


class SearchPipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = Settings(
            app_name="test",
            api_prefix="/api/v1",
            frontend_origin="http://localhost:3000",
            sample_catalog_path=Path(__file__).resolve().parents[1]
            / "data"
            / "sample_catalog.json",
            catalog_snapshot_path=Path(__file__).resolve().parents[1]
            / "data"
            / "runtime-test"
            / "catalog_snapshot.json",
            sync_state_path=Path(__file__).resolve().parents[1]
            / "data"
            / "runtime-test"
            / "sync_state.json",
            catalog_mode="sample",
            default_limit=6,
            akeneo=AkeneoSettings(
                base_url=None,
                username=None,
                password=None,
                client_id=None,
                client_secret=None,
                preferred_locale="fr_FR",
                fallback_locale="en_US",
                page_limit=100,
                max_products=None,
            ),
            typesense=TypesenseSettings(
                url=None,
                api_key=None,
                collection_name="products",
                import_batch_size=250,
                candidate_multiplier=8,
            ),
            groq=GroqSettings(
                api_key=None,
                base_url="https://api.groq.com/openai/v1",
                model="openai/gpt-oss-20b",
                timeout_seconds=20,
            ),
            cohere=CohereSettings(
                api_key=None,
                base_url="https://api.cohere.com/v2",
                model="rerank-v4.0-fast",
                timeout_seconds=20,
            ),
        )
        cls.search_application = build_search_application(cls.settings)
        cls.search_application.reset_local_index()
        cls.search_application.reindex(source="sample")

    def test_pc_20_pouces_prefers_computer_products(self) -> None:
        result = self.search_application.search("pc 20 pouces", limit=3)

        self.assertEqual(result.structured_query.filters["screen_size"], ["20 pouces"])
        self.assertGreaterEqual(len(result.hits), 1)
        self.assertIn("pc", result.hits[0].product.name.lower())

    def test_phone_query_hits_smartphone(self) -> None:
        result = self.search_application.search("telephone bleu 128 go", limit=3)

        top_names = [hit.product.name.lower() for hit in result.hits]
        self.assertTrue(any("smartphone" in name for name in top_names))

    def test_brand_filter_is_detected(self) -> None:
        result = self.search_application.search("novatech pc", limit=3)

        self.assertIn("brand", result.structured_query.filters)
        self.assertIn("NovaTech", result.structured_query.filters["brand"])

    def test_short_follow_up_uses_previous_context(self) -> None:
        first = self.search_application.search(
            "ordinateur portable",
            limit=6,
            session_id="session-memory",
        )
        second = self.search_application.search(
            "20 pouces",
            limit=6,
            session_id="session-memory",
        )

        self.assertFalse(first.structured_query.context_used)
        self.assertTrue(second.structured_query.context_used)
        self.assertIn("screen_size", second.structured_query.filters)
        self.assertIn("20 pouces", second.structured_query.filters["screen_size"])
        self.assertTrue(any("portable" in hit.product.name.lower() for hit in second.hits))

    def test_same_session_id_does_not_mix_between_visitors(self) -> None:
        self.search_application.search(
            "ordinateur portable",
            limit=6,
            session_id="shared-session",
            visitor_id="visitor-a",
        )
        isolated = self.search_application.search(
            "20 pouces",
            limit=6,
            session_id="shared-session",
            visitor_id="visitor-b",
        )

        self.assertFalse(isolated.structured_query.context_used)

    def test_iphone_follow_up_keeps_context_and_maps_gb_storage(self) -> None:
        products = [
            Product(
                source_id="iphone-15-black-128",
                sku="IPH15-BLK-128",
                name="Apple iPhone 15",
                description="Smartphone noir avec 128 Go de stockage.",
                brand="Apple",
                categories=["iPhone"],
                attributes={
                    "product_family": ["smartphone"],
                    "color": ["Noir"],
                    "storage": ["128 go"],
                },
            )
        ]
        query_service = QueryUnderstandingService()

        first = query_service.understand("iphone", products)
        second = query_service.understand(
            "15 noir 128 GB",
            products,
            previous_query=first,
        )

        self.assertIn("product_family", first.filters)
        self.assertEqual(first.filters["product_family"], ["smartphone"])
        self.assertTrue(second.context_used)
        self.assertIn("iphone", second.normalized_text)
        self.assertIn("128 go", second.normalized_text)
        self.assertEqual(second.filters.get("color"), ["Noir"])
        self.assertEqual(second.filters.get("storage"), ["128 go"])

    def test_storage_filter_accepts_gb_alias(self) -> None:
        products = [
            Product(
                source_id="iphone-15-blue-128",
                sku="IPH15-BLU-128",
                name="Apple iPhone 15",
                description="Smartphone bleu avec 128 Go de stockage.",
                brand="Apple",
                categories=["iPhone"],
                attributes={
                    "product_family": ["smartphone"],
                    "color": ["Bleu"],
                    "storage": ["128 go"],
                },
            )
        ]
        query_service = QueryUnderstandingService()

        result = query_service.understand("iphone 128 GB", products)

        self.assertEqual(result.filters.get("storage"), ["128 go"])

    def test_specs_only_query_infers_computer_family(self) -> None:
        query_service = QueryUnderstandingService()

        result = query_service.understand("512 sdd 16gb", [])

        self.assertEqual(result.filters.get("product_family"), ["computer"])
        self.assertEqual(result.filters.get("storage"), ["512 go"])
        self.assertIn("ordinateur", result.normalized_text)

    def test_follow_up_color_replaces_previous_color_in_context(self) -> None:
        products = [
            Product(
                source_id="iphone-blue",
                sku="IPH-BLUE",
                name="Apple iPhone 15 Bleu",
                description="Smartphone bleu.",
                brand="Apple",
                categories=["iPhone"],
                attributes={"product_family": ["smartphone"], "color": ["Bleu"]},
            )
        ]
        query_service = QueryUnderstandingService()

        first = query_service.understand("iphone", products)
        second = query_service.understand("rouge", products, previous_query=first)
        third = query_service.understand("bleu", products, previous_query=second)

        self.assertTrue(second.context_used)
        self.assertEqual(second.filters.get("color"), ["Rouge"])
        self.assertTrue(third.context_used)
        self.assertEqual(third.filters.get("color"), ["Bleu"])
        self.assertNotIn("Rouge", third.filters.get("color", []))
        self.assertNotIn("rouge", third.normalized_text)

    def test_brand_then_product_family_follow_up_keeps_context(self) -> None:
        products = [
            Product(
                source_id="dell-laptop",
                sku="DELL-LAP",
                name="Dell Latitude ordinateur portable",
                description="Laptop Dell.",
                brand="Dell",
                categories=["Ordinateurs portables"],
                attributes={"product_family": ["computer"]},
            ),
            Product(
                source_id="dell-keyboard",
                sku="DELL-KBD",
                name="Dell clavier",
                description="Accessoire Dell.",
                brand="Dell",
                categories=["Claviers"],
                attributes={"product_family": ["computer"]},
            ),
            Product(
                source_id="hp-laptop",
                sku="HP-LAP",
                name="HP ordinateur portable",
                description="Laptop HP.",
                brand="HP",
                categories=["Ordinateurs portables"],
                attributes={"product_family": ["computer"]},
            ),
        ]
        query_service = QueryUnderstandingService()
        hybrid_search = HybridSearchService()

        first = query_service.understand("dell", products)
        second = query_service.understand("ordinateur", products, previous_query=first)
        hits = hybrid_search.search(second, products, limit=3)

        self.assertTrue(second.context_used)
        self.assertEqual(second.filters.get("brand"), ["Dell"])
        self.assertEqual(second.filters.get("product_family"), ["computer"])
        self.assertEqual(hits[0].product.sku, "DELL-LAP")

    def test_product_family_then_brand_follow_up_keeps_context(self) -> None:
        products = [
            Product(
                source_id="dell-laptop",
                sku="DELL-LAP",
                name="Dell Latitude ordinateur portable",
                description="Laptop Dell.",
                brand="Dell",
                categories=["Ordinateurs portables"],
                attributes={"product_family": ["computer"]},
            ),
            Product(
                source_id="hp-laptop",
                sku="HP-LAP",
                name="HP ordinateur portable",
                description="Laptop HP.",
                brand="HP",
                categories=["Ordinateurs portables"],
                attributes={"product_family": ["computer"]},
            ),
            Product(
                source_id="dell-monitor",
                sku="DELL-MON",
                name="Dell ecran",
                description="Moniteur Dell.",
                brand="Dell",
                categories=["Écrans"],
                attributes={"product_family": ["monitor"]},
            ),
        ]
        query_service = QueryUnderstandingService()
        hybrid_search = HybridSearchService()

        first = query_service.understand("ordinateur", products)
        second = query_service.understand("dell", products, previous_query=first)
        hits = hybrid_search.search(second, products, limit=3)

        self.assertTrue(second.context_used)
        self.assertEqual(second.filters.get("brand"), ["Dell"])
        self.assertEqual(second.filters.get("product_family"), ["computer"])
        self.assertEqual(hits[0].product.sku, "DELL-LAP")

    def test_query_with_two_colors_behaves_like_or_filter(self) -> None:
        products = [
            Product(
                source_id="iphone-red",
                sku="IPH-RED",
                name="Apple iPhone 15 Rouge",
                description="Smartphone rouge.",
                brand="Apple",
                categories=["iPhone"],
                attributes={"product_family": ["smartphone"], "color": ["Rouge"]},
            ),
            Product(
                source_id="iphone-blue",
                sku="IPH-BLUE",
                name="Apple iPhone 15 Bleu",
                description="Smartphone bleu.",
                brand="Apple",
                categories=["iPhone"],
                attributes={"product_family": ["smartphone"], "color": ["Bleu"]},
            ),
            Product(
                source_id="iphone-black",
                sku="IPH-BLACK",
                name="Apple iPhone 15 Noir",
                description="Smartphone noir.",
                brand="Apple",
                categories=["iPhone"],
                attributes={"product_family": ["smartphone"], "color": ["Noir"]},
            ),
        ]
        query_service = QueryUnderstandingService()
        hybrid_search = HybridSearchService()

        structured_query = query_service.understand("iphone rouge ou bleu", products)
        hits = hybrid_search.search(structured_query, products, limit=5)
        hit_names = [hit.product.name for hit in hits]

        self.assertIn("Rouge", structured_query.filters.get("color", []))
        self.assertIn("Bleu", structured_query.filters.get("color", []))
        self.assertNotIn("rouge", structured_query.normalized_text)
        self.assertNotIn("bleu", structured_query.normalized_text)
        self.assertIn("Apple iPhone 15 Rouge", hit_names)
        self.assertIn("Apple iPhone 15 Bleu", hit_names)
        self.assertNotIn("Apple iPhone 15 Noir", hit_names)

    def test_multi_color_diversification_interleaves_requested_values(self) -> None:
        structured_query = StructuredQuery(
            raw_query="iphone rouge ou bleu",
            normalized_text="smartphone iphone",
            keywords=["iphone"],
            filters={"product_family": ["smartphone"], "color": ["Rouge", "Bleu"]},
            boost_terms=[],
            intent="search",
            explanation="",
        )
        hits = [
            SearchHit(
                product=Product(
                    source_id="red-1",
                    sku="RED-1",
                    name="iPhone Rouge 1",
                    description="",
                    brand="Apple",
                    categories=["iPhone"],
                    attributes={"product_family": ["smartphone"], "color": ["Rouge"]},
                ),
                score=0.9,
                lexical_score=0.9,
                semantic_score=0.9,
                matched_filters={"color": ["Rouge"]},
            ),
            SearchHit(
                product=Product(
                    source_id="red-2",
                    sku="RED-2",
                    name="iPhone Rouge 2",
                    description="",
                    brand="Apple",
                    categories=["iPhone"],
                    attributes={"product_family": ["smartphone"], "color": ["Rouge"]},
                ),
                score=0.88,
                lexical_score=0.88,
                semantic_score=0.88,
                matched_filters={"color": ["Rouge"]},
            ),
            SearchHit(
                product=Product(
                    source_id="blue-1",
                    sku="BLUE-1",
                    name="iPhone Bleu 1",
                    description="",
                    brand="Apple",
                    categories=["iPhone"],
                    attributes={"product_family": ["smartphone"], "color": ["Bleu"]},
                ),
                score=0.87,
                lexical_score=0.87,
                semantic_score=0.87,
                matched_filters={"color": ["Bleu"]},
            ),
        ]

        diversified = self.search_application._diversify_multi_value_filters(
            structured_query,
            hits,
            limit=3,
        )

        self.assertEqual([hit.product.sku for hit in diversified], ["RED-1", "BLUE-1", "RED-2"])

    def test_rerank_query_keeps_structured_filters_for_multi_value_intent(self) -> None:
        structured_query = StructuredQuery(
            raw_query="iphone rouge ou bleu",
            normalized_text="smartphone iphone",
            keywords=["iphone", "rouge", "bleu"],
            filters={"product_family": ["smartphone"], "color": ["Rouge", "Bleu"]},
            boost_terms=["apple"],
            intent="search",
            explanation="",
        )
        reranker = RerankerService()

        query_text = reranker._build_rerank_query(structured_query)

        self.assertIn("smartphone iphone", query_text)
        self.assertIn("product_family smartphone", query_text)
        self.assertIn("color Rouge Bleu", query_text)

    def test_typesense_candidate_page_size_is_capped(self) -> None:
        service = TypesenseService(self.settings.typesense)

        self.assertEqual(service._candidate_page_size(6, has_filters=True), 120)
        self.assertEqual(service._candidate_page_size(60, has_filters=True), 250)
        self.assertEqual(service._candidate_page_size(60, has_filters=False), 250)

    def test_context_summary_prefers_human_readable_values(self) -> None:
        query_service = QueryUnderstandingService()

        summary = query_service._build_context_summary(
            {"brand": ["Apple"], "product_family": ["smartphone"], "storage": ["128 go"]},
            ["iphone"],
        )

        self.assertEqual(summary, "Apple | smartphone | 128 go")

    def test_exact_sku_query_returns_direct_match(self) -> None:
        app = build_search_application(self.settings)
        product = Product(
            source_id="sony-pulse-elite",
            sku="P5378552",
            name="Sony PULSE Elite",
            description="Micro-casque Sony.",
            brand="Sony",
            categories=["Casques"],
        )
        app.catalog_store.replace([product])
        app.catalog_facets = app.query_understanding._build_catalog_lookup([product]).snapshot

        result = app.search("P5378552", limit=6)

        self.assertEqual(result.total_candidates, 1)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].product.sku, "P5378552")
        self.assertEqual(result.structured_query.normalized_text, "p5378552")
        self.assertIn("Reference exacte reconnue", result.assistant_message)

    def test_sku_prefixed_query_returns_direct_match(self) -> None:
        app = build_search_application(self.settings)
        product = Product(
            source_id="sony-pulse-elite",
            sku="P5378552",
            name="Sony PULSE Elite",
            description="Micro-casque Sony.",
            brand="Sony",
            categories=["Casques"],
        )
        app.catalog_store.replace([product])
        app.catalog_facets = app.query_understanding._build_catalog_lookup([product]).snapshot

        result = app.search("SKU: P5378552", limit=6)

        self.assertEqual(result.total_candidates, 1)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].product.sku, "P5378552")
        self.assertEqual(result.structured_query.normalized_text, "p5378552")
        self.assertEqual(result.structured_query.keywords, ["p5378552"])

    def test_computer_query_demotes_accessory_results(self) -> None:
        hybrid_search = HybridSearchService()
        structured_query = StructuredQuery(
            raw_query="ordinateur dell",
            normalized_text="dell ordinateur",
            keywords=["ordinateur", "dell"],
            filters={"brand": ["Dell"], "product_family": ["computer"]},
            boost_terms=[],
            intent="search",
            explanation="",
        )
        products = [
            Product(
                source_id="kbd-1",
                sku="KBD-1",
                name="Dell - Clavier de remplacement pour ordinateur portable",
                description="Clavier accessoire.",
                brand="Dell",
                categories=["Claviers"],
                attributes={"product_family": ["computer"]},
            ),
            Product(
                source_id="lap-1",
                sku="LAP-1",
                name="Dell Latitude 7450 ordinateur portable 16 Go 512 Go",
                description="Laptop Dell.",
                brand="Dell",
                categories=["Ordinateurs portables"],
                attributes={"product_family": ["laptop"]},
            ),
        ]

        hits = hybrid_search.search(structured_query, products, limit=2)

        self.assertEqual(hits[0].product.sku, "LAP-1")

    def test_normalizer_uses_attribute_labels_for_live_like_payloads(self) -> None:
        normalizer = AkeneoCatalogNormalizer(preferred_locale="fr_FR", fallback_locale="en_US")
        raw_item = {
            "uuid": "abc-123",
            "enabled": True,
            "categories": ["chairs"],
            "values": {
                "sku": [
                    {
                        "locale": None,
                        "scope": None,
                        "data": "CHAIR-001",
                        "attribute_type": "pim_catalog_identifier",
                    }
                ],
                "x_name": [
                    {
                        "locale": "fr_FR",
                        "scope": None,
                        "data": "Chaise ergonomique Alto",
                        "attribute_type": "pim_catalog_text",
                    }
                ],
                "x_brand": [
                    {
                        "locale": None,
                        "scope": None,
                        "data": "alto_brand",
                        "attribute_type": "pim_catalog_simpleselect",
                        "linked_data": {"labels": {"fr_FR": "Alto"}},
                    }
                ],
                "x_desc": [
                    {
                        "locale": "fr_FR",
                        "scope": None,
                        "data": "Chaise de bureau avec support lombaire.",
                        "attribute_type": "pim_catalog_textarea",
                    }
                ],
                "x_price": [
                    {
                        "locale": None,
                        "scope": None,
                        "data": [{"amount": "199.00", "currency": "EUR"}],
                        "attribute_type": "pim_catalog_price_collection",
                    }
                ],
                "x_color": [
                    {
                        "locale": None,
                        "scope": None,
                        "data": "black",
                        "attribute_type": "pim_catalog_simpleselect",
                        "linked_data": {"labels": {"fr_FR": "Noir"}},
                    }
                ],
            },
        }
        metadata = {
            "x_name": AttributeMetadata(
                code="x_name",
                type="pim_catalog_text",
                labels={"fr_FR": "Nom du produit"},
            ),
            "x_brand": AttributeMetadata(
                code="x_brand",
                type="pim_catalog_simpleselect",
                labels={"fr_FR": "Marque"},
            ),
            "x_desc": AttributeMetadata(
                code="x_desc",
                type="pim_catalog_textarea",
                labels={"fr_FR": "Description"},
            ),
            "x_price": AttributeMetadata(
                code="x_price",
                type="pim_catalog_price_collection",
                labels={"fr_FR": "Prix"},
            ),
            "x_color": AttributeMetadata(
                code="x_color",
                type="pim_catalog_simpleselect",
                labels={"fr_FR": "Couleur"},
            ),
        }

        product = normalizer.normalize(
            raw_item,
            attribute_metadata=metadata,
            category_labels={"chairs": "Chaises"},
        )

        self.assertEqual(product.source_id, "abc-123")
        self.assertEqual(product.sku, "CHAIR-001")
        self.assertEqual(product.name, "Chaise ergonomique Alto")
        self.assertEqual(product.brand, "Alto")
        self.assertEqual(product.description, "Chaise de bureau avec support lombaire.")
        self.assertEqual(product.price, 199.0)
        self.assertEqual(product.categories, ["Chaises"])
        self.assertEqual(product.attributes["Couleur"], ["Noir"])

    def test_typesense_document_roundtrip(self) -> None:
        product = self.search_application.list_products()[0]
        service = TypesenseService(self.settings.typesense)

        document = service._product_to_document(product)
        rebuilt = service._document_to_product(document)

        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.source_id, product.source_id)
        self.assertEqual(rebuilt.sku, product.sku)
        self.assertEqual(rebuilt.name, product.name)
        self.assertEqual(rebuilt.brand, product.brand)
        self.assertEqual(rebuilt.categories, product.categories)


if __name__ == "__main__":
    unittest.main()
