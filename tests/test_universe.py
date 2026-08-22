"""Tests for config.universe — the tradeable token universe."""

from config.universe import universe, TRADEABLE_UNIVERSE


class TestUniverse:
    def test_universe_returns_tuple(self):
        assert isinstance(universe(), tuple)
        assert universe() == TRADEABLE_UNIVERSE

    def test_btc_eth_present(self):
        # The existing pair must remain in the universe.
        assert "BTCUSDT" in universe()
        assert "ETHUSDT" in universe()

    def test_matic_excluded(self):
        # MATIC was delisted/rebranded to POL on Binance (Sep 2024).
        # Including it would create a mid-backtest seam.
        assert "MATICUSDT" not in universe()
        assert "POLUSDT" not in universe()

    def test_all_usdt_pairs(self):
        # Momentum ranking compares like-for-like; all quote in USDT.
        assert all(s.endswith("USDT") for s in universe())

    def test_enough_for_cross_section(self):
        # Need a real cross-section to rank, not a coin flip.
        assert len(universe()) >= 8

    def test_no_duplicates(self):
        assert len(universe()) == len(set(universe()))
