"""Live market data sources. Each submodule wraps one exchange/odds
provider's HTTP API; nothing here is imported by the core compute modules
(models/engine/strategies/montecarlo) -- evhedge works fully offline via
config_io without any of this."""
