"""Loopback-only upstreams for the real ERP OpenAI and Shopify HTTP clients."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from erp_core.ai_engine.ingestion import DeterministicHashEmbeddingProvider

_CITATION = re.compile(r" \[[0-9a-f-]{36}#[0-9a-f-]{36}\]\.$")


def shopify_order(fulfilled: bool) -> dict[str, Any]:
    def money(amount: str) -> dict[str, Any]:
        return {"shopMoney": {"amount": amount, "currencyCode": "USD"}}

    return {
        "id": f"gid://shopify/Order/{os.environ['E2E_SHOPIFY_ORDER_ID']}",
        "name": "#SH-1042",
        "updatedAt": datetime.now(UTC).isoformat(),
        "currencyCode": "USD",
        "currentSubtotalPriceSet": money("89.70"),
        "currentTotalPriceSet": money("89.90"),
        "totalShippingPriceSet": money("0.20"),
        "shippingAddress": {"countryCodeV2": "US"},
        "customer": {"firstName": "Elena", "lastName": "Vance", "email": "elena@example.com"},
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "FULFILLED" if fulfilled else "UNFULFILLED",
        "lineItems": {
            "nodes": [
                {
                    "sku": "LED-COB-24V-5M",
                    "quantity": 3,
                    "originalTotalSet": money("89.70"),
                }
            ]
        },
    }


class ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/embeddings":
            embed = DeterministicHashEmbeddingProvider().embed_one
            response = {
                "data": [
                    {"index": i, "embedding": embed(value)} for i, value in enumerate(body["input"])
                ]
            }
        elif self.path == "/v1/chat/completions":
            user = body["messages"][-1]["content"]
            if user.startswith("Translate to "):
                reply = (
                    "Hello, where is my order?" if "dónde" in user else "Your order is on its way."
                )
            else:
                context, _, _ = user.partition("\n\nUser Input:\n")
                reply = next((line for line in context.splitlines() if _CITATION.search(line)), "")
                if not reply:
                    self.send_error(422, "No cited context supplied to the fixture")
                    return
            response = {
                "id": "chatcmpl-live-e2e",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 128, "completion_tokens": 24, "total_tokens": 152},
            }
        elif self.path.startswith("/admin/api/") and self.path.endswith("/graphql.json"):
            token = self.headers.get("X-Shopify-Access-Token")
            if token != "demo-token" or "orders(" not in body["query"]:
                self.send_error(422, "Unexpected Shopify GraphQL request")
                return
            server = self.server
            assert isinstance(server, ProviderServer)
            # The test waits for the first order to commit before sending orders/updated.
            order = shopify_order(server.shopify_order_read)
            server.shopify_order_read = True
            response = {
                "data": {
                    "orders": {
                        "nodes": [order],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        else:
            self.send_error(404)
            return
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ProviderServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), ProviderHandler)
        self.shopify_order_read = False
