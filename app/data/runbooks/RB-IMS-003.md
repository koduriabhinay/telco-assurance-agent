---
id: RB-IMS-003
title: Cx Diameter failure between S-CSCF and HSS causing VoLTE/VoNR registration failures
domain: IMS
alarm_codes: DIAMETER_PEER_DOWN, CX_TIMEOUT, SIP_5XX_HIGH
actions: failover_diameter_peer, restart_diameter_stack
---
# Cx Diameter failure between S-CSCF and HSS

## Symptoms
- HSS reports DIAMETER_PEER_DOWN for the S-CSCF Cx connection (DWR/DWA watchdog timeout).
- S-CSCF raises CX_TIMEOUT on MAR/SAR (Multimedia-Auth and Server-Assignment requests).
- P-CSCF sees SIP 503 on REGISTER; VoLTE/VoNR registration and call setup success drop.

## Diagnosis
1. The P-CSCF 503 errors are downstream symptoms. Look for the earliest Diameter alarm on Cx.
2. Confirm the HSS is reachable from I-CSCF. If I-CSCF Cx is fine, the fault is the S-CSCF to HSS peer.

## Remediation
- `failover_diameter_peer`: point the S-CSCF to the secondary HSS front-end. Low risk.
- `restart_diameter_stack`: restart the Diameter stack on the S-CSCF. Medium risk, brief registration outage.

## Verification
- Cx peer state OPEN, SIP 503 rate below 1%, IMS registration success above 99%.
