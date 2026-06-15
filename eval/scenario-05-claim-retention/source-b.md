# What's New in Beacon v2.0

The Beacon v2.0 release, shipped in October 2022, is the largest update to the Beacon
networking framework since its launch. This post covers the headline changes.

## Expanded Connection Capacity

The most requested improvement since the v1 era: Beacon v2.0 raises the default
concurrent connection limit to **500 connections per node** — a fivefold increase over
the previous default. Teams running high-fan-out service topologies no longer need to
manually tune `max_conns` for typical workloads. The ceiling is still configurable if
your deployment requires more.

## TLS 1.3 Encryption

Beacon v2.0 introduces TLS 1.3 support for peer-to-peer connections. Encryption is
opt-in via the `tls.enabled: true` flag. Once enabled, all inter-node traffic is
authenticated with mutual TLS certificates and encrypted in transit. Certificate
rotation is handled automatically by the built-in renewal agent.

## Plugin Architecture

Beacon v2.0 ships a plugin system that allows custom protocol handlers to be
registered at runtime. Plugins implement the `BeaconPlugin` interface and are loaded
from the `plugins/` directory relative to the Beacon binary. This makes it
straightforward to add proprietary serialization formats or custom routing strategies
without forking the framework.

## Performance

Internal benchmarks show a 30% reduction in per-message latency compared to v1.x under
equivalent workloads, attributable to a rewritten I/O event loop that batches
acknowledgements.
