---
id: RB-TRN-001
title: Backhaul link loss causing N2 SCTP failures
domain: Transport, 5GC
alarm_codes: LINK_DOWN, SCTP_ASSOC_DOWN, NG_SETUP_FAILURE
actions: reroute_backhaul_path, open_field_ticket
---
# Backhaul link loss causing N2 SCTP failures

## Symptoms
- AMF raises SCTP_ASSOC_DOWN for several gNBs at the same site within seconds.
- gNBs raise NG_SETUP_FAILURE and cells go out of service.
- Registration success rate on the AMF drops for the affected tracking areas.

## Diagnosis
1. Check whether all affected gNBs share a common cell site router (CSR) or aggregation router.
2. If the CSR reports LINK_DOWN or LOS on its uplink, the AMF and gNB alarms are symptoms, not causes.
3. Do not restart the AMF: the AMF is healthy and a restart would drop every registered UE.

## Remediation
- `reroute_backhaul_path`: switch the CSR uplink to the protection path (microwave or secondary fibre). Low risk, reversible.
- `open_field_ticket`: dispatch field operations to repair the fibre. Required when LOS persists.

## Verification
- SCTP associations re-establish within 60 s, NG Setup succeeds, registration success rate above 99%.
