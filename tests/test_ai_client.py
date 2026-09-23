import asyncio

from ai_client import NebiusClient


def test_embed_orders_by_index_and_posts_to_embeddings():
    client = NebiusClient(api_key="k", base_url="https://example.test/v1", model="m")
    seen = {}

    async def fake_post(payload, endpoint=None):
        seen["payload"], seen["endpoint"] = payload, endpoint
        return {"data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]}

    client._post_with_retries = fake_post
    vectors = asyncio.run(client.embed(["a", "b"], model="emb", dimensions=2))

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["endpoint"] == "https://example.test/v1/embeddings"
    assert seen["payload"] == {"model": "emb", "input": ["a", "b"], "dimensions": 2}
    asyncio.run(client.aclose())


def test_embed_empty_input_makes_no_call():
    client = NebiusClient(api_key="k", base_url="https://example.test/v1", model="m")
    assert asyncio.run(client.embed([], model="emb", dimensions=2)) == []
    asyncio.run(client.aclose())
