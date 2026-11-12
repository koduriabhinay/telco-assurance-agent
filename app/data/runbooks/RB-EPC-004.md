---
id: RB-EPC-004
title: S6a authentication failures through the DRA (4G attach failures)
domain: EPC
alarm_codes: DIAMETER_ROUTE_FAIL, S6A_AIR_TIMEOUT, ATTACH_FAILURE_HIGH
actions: failover_diameter_peer, update_dra_route
---
# S6a failures through the DRA

## Symptoms
- DRA cannot route to the HSS realm (DIAMETER_ROUTE_FAIL).
- MME sees S6a AIR (Authentication-Information-Request) timeouts.
- EPS attach success rate drops; LTE subscribers cannot attach.

## Diagnosis
1. When the DRA alarm is earliest, the HSS peer or DRA routing table is at fault, not the MME.
2. Check whether HSS is reachable directly. If yes, the DRA route entry is stale.

## Remediation
- `failover_diameter_peer`: route S6a to the geo-redundant HSS. Low risk.
- `update_dra_route`: correct the realm routing entry on the DRA. Medium risk.

## Verification
- AIR success above 99.5%, attach success above 98%.
