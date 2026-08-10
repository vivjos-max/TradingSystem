"""Broker-neutral keys shared by the API and worker composition roots."""

ACCOUNT_KEY = "primary"
OPTION_CHAIN_KEY = "nifty-current"
NEAREST_FUTURE_QUOTE_KEY = "nifty-nearest-future"


__all__ = ["ACCOUNT_KEY", "NEAREST_FUTURE_QUOTE_KEY", "OPTION_CHAIN_KEY"]
