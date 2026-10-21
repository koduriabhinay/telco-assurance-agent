---
id: RB-UPF-002
title: UPF user-plane overload and N4 PFCP heartbeat failures
domain: 5GC
alarm_codes: CPU_OVERLOAD, PFCP_HEARTBEAT_FAIL, PDU_SESSION_REJECT_HIGH
actions: scale_out_upf, drain_upf_pod, restart_upf_pod
---
# UPF overload and N4 PFCP heartbeat failures

## Symptoms
- UPF data-plane pod CPU above 90%.
- SMF raises PFCP_HEARTBEAT_FAIL on the N4 interface towards that UPF.
- PDU session establishment rejects rise with cause "insufficient resources".

## Diagnosis
1. Correlate the UPF CPU alarm time with the first PFCP heartbeat failure. If CPU saturation came first, the UPF is the root cause.
2. Check throughput KPIs: a traffic surge (event, stadium) points to capacity, a flat throughput with high CPU points to a software fault.

## Remediation
- `scale_out_upf`: add a data-plane replica through the CNF manager. Low risk.
- `drain_upf_pod`: move sessions away from the hot pod. Medium risk.
- `restart_upf_pod`: last resort, drops active sessions on that pod. High risk.

## Verification
- CPU below 70%, PFCP heartbeats stable for 10 min, PDU session success rate above 98%.
