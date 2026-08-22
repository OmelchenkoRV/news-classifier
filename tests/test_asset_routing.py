"""Tests for config.asset_routing and routing in schedule_outcomes."""

from config.asset_routing import (
    assets_for_category, all_routed_assets, BTC, ETH, WTI, GOLD, KNOWN_ASSETS,
)


class TestAssetRouting:
    def test_geopolitical_routes_to_oil_and_gold(self):
        got = assets_for_category("geopolitical")
        assert WTI in got and GOLD in got
        assert BTC not in got and ETH not in got

    def test_macro_routes_cross_asset(self):
        got = assets_for_category("macro")
        assert BTC in got and ETH in got and WTI in got and GOLD in got

    def test_regulatory_is_crypto_only(self):
        got = assets_for_category("regulatory")
        assert BTC in got and ETH in got
        assert WTI not in got and GOLD not in got

    def test_reactive_categories_route_to_nothing(self):
        for cat in ("opinion", "promotion", "price_commentary",
                    "technical_analysis", "uncategorized"):
            assert assets_for_category(cat) == ()

    def test_unknown_category_fails_safe(self):
        assert assets_for_category("some_new_category_v3") == ()

    def test_all_routed_assets_subset_of_known(self):
        assert all_routed_assets().issubset(KNOWN_ASSETS)

    def test_all_routed_assets_contents(self):
        assert all_routed_assets() == frozenset({BTC, ETH, WTI, GOLD})
