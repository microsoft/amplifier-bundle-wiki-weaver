# Beacon Networking Framework

**Beacon** is an open-source, lightweight peer-to-peer networking library designed for
building distributed service meshes. It exposes a clean connection API that handles
discovery, handshaking, and message routing transparently, letting application code
focus on business logic rather than network plumbing.

## Origin and History

Beacon was first released in March 2019 by Redway Systems as an open-source project
under the MIT license. The initial release targeted microservices teams who needed a
lightweight alternative to heavier service-mesh solutions that required a dedicated
sidecar proxy on every host.

## Connection Limits

By default, each Beacon node supports up to 100 concurrent connections. This ceiling
was chosen to keep memory overhead predictable on modest hardware. It can be raised at
any time via the `max_conns` configuration key without restarting the node.

## Configuration

Beacon configuration is defined in YAML files. The primary config file is `beacon.yaml`,
which must be placed at the project root. All runtime parameters — including timeouts,
retry intervals, and the `max_conns` ceiling — are read from this file at startup. Hot
reloading of configuration changes is not supported in v1.x.

## Routing

Beacon uses a consistent-hash ring for peer discovery. Messages are routed by hashing
the destination service ID, and the ring rebalances automatically when nodes join or
leave the cluster. This makes Beacon suitable for workloads where service topology
changes frequently.

## Reliability

Beacon retries failed messages up to three times with exponential backoff before
surfacing an error to the caller. Retries are transparent to application code.
