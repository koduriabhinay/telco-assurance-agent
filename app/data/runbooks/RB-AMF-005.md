---
id: RB-AMF-005
title: AMF overload and registration storm
domain: 5GC
alarm_codes: AMF_OVERLOAD, REGISTRATION_REJECT_HIGH
actions: enable_overload_control, scale_out_amf
---
# AMF overload and registration storm

## Symptoms
- AMF CPU high, NAS registration rejects with cause 22 (congestion).
- Happens after a large RAN outage recovers and many UEs re-register at once.

## Diagnosis
- Distinguish from transport faults: in an overload, SCTP associations stay up.

## Remediation
- `enable_overload_control`: send NGAP Overload Start to gNBs. Low risk.
- `scale_out_amf`: add AMF set members. Low risk.
