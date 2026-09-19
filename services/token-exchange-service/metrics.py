from prometheus_client import Counter, Histogram

token_exchange_total = Counter(
    "cdp_token_exchange_total",
    "Token exchange results",
    ["result"]  # hit / miss / fail
)

token_exchange_latency = Histogram(
    "cdp_token_exchange_latency_seconds",
    "Token exchange latency in seconds",
)
