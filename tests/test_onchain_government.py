import unittest
from unittest.mock import patch

from backend.config import _parse_exchange_btc_addresses, config
from backend.data.onchain_client import OnchainClient


client = OnchainClient()


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _HttpClient:
    def __init__(self, responses):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **_kwargs):
        return _Response(self.responses[url])


class GovernmentDepositTests(unittest.IsolatedAsyncioTestCase):
    def test_exchange_address_parser_merges_repeated_exchange_entries(self):
        self.assertEqual(
            _parse_exchange_btc_addresses("Coinbase:bc1a|bc1b,Coinbase:3abc"),
            {"Coinbase": ("bc1a", "bc1b", "3abc")},
        )

    async def test_btc_requires_both_government_and_exchange_address_lists(self):
        client = OnchainClient()
        with (
            patch.object(config, "GOVERNMENT_BTC_ADDRESSES", ("bc1government",)),
            patch.object(config, "EXCHANGE_BTC_ADDRESSES", {}),
        ):
            result = await client.get_government_exchange_deposits("BTC")
        self.assertFalse(result["chain_confirmed"])
        self.assertIn("EXCHANGE_BTC_ADDRESSES", result["reason"])

    async def test_btc_destination_match_is_reported_as_chain_confirmation(self):
        government = "bc1government"
        exchange = "bc1coinbase"
        txid = "a" * 64
        url = f"{client.MEMPOOL_BASE}/address/{government}/txs"
        payload = [{
            "txid": txid,
            "status": {"block_time": 1_900_000_000},
            "vin": [{"prevout": {"scriptpubkey_address": government}}],
            "vout": [
                {"scriptpubkey_address": exchange, "value": 123_456_789},
                {"scriptpubkey_address": "bc1change", "value": 1},
            ],
        }]
        with (
            patch.object(config, "GOVERNMENT_BTC_ADDRESSES", (government,)),
            patch.object(config, "EXCHANGE_BTC_ADDRESSES", {"Coinbase": (exchange,)}),
            patch("backend.data.onchain_client.make_client", return_value=_HttpClient({url: payload})),
            patch("backend.data.onchain_client.time.time", return_value=1_900_000_100),
        ):
            result = await client.get_government_exchange_deposits("BTC", lookback_hours=24)
        self.assertTrue(result["chain_confirmed"])
        self.assertEqual(result["government_exchange_deposit_count"], 1)
        self.assertEqual(result["deposits"][0]["exchange"], "Coinbase")
        self.assertEqual(result["government_exchange_deposit_value_btc"], 1.23456789)

if __name__ == "__main__":
    unittest.main()
