# Rough Order of Magnitude (ROM): Customer 360 Profile Unification

**Prepared by:** Enterprise Architecture, Customer Domain
**Date:** 2026-10-12
**Status:** Draft for resource allocation review
**Synthetic sample for Delivery Twin demos. All names and figures are fictional.**

## 1. Request summary

Leadership wants one trusted view of every customer across the business. Sales, service and marketing teams
currently get different answers because customer records live in separate systems. The target is a single
customer profile that is available to the contact center and to downstream analytics.

## 2. Scope

In scope:
- Consolidate customer records from the CRM and the master data hub (MDM).
- Build an identity resolution service that matches and merges duplicate customers.
- Expose a profile API for the contact center agent console.
- Migrate and reconcile existing customer data, with a reconciliation report.

Out of scope:
- Replacing the CRM or the MDM product.
- Marketing segmentation changes.
- Self-service portal redesign.

## 3. Systems affected

- CRM
- MDM (customer master)
- Identity resolution service (new)
- Profile API (new)
- Service management (read-only integration)

## 4. Effort estimate (ROM, plus or minus 30 percent)

| Work package | Hours |
|---|---|
| Discovery and design | 400 |
| CRM and MDM integration | 1,200 |
| Identity resolution service | 1,300 |
| Profile API | 800 |
| Data migration and reconciliation | 700 |
| Testing, cutover and hand-over | 900 |
| **Total (most likely)** | **5,300** |

Pessimistic case (P80), including integration contingency: 6,500 hours.

## 5. Proposed team

- 1 Lead Engineer
- 2 Delivery Engineers
- 2 Data Engineers
- 1 Privacy Engineer (part time during design and cutover)
- 1 Platform Engineer (part time)

## 6. Complexity

High. Several systems are involved and matching rules must be agreed across business units.

## 7. Dependencies

- Privacy review of how customer identifiers are matched and stored.
- Consent management service must be able to supply consent flags for every merged profile.
- CRM to MDM integration must be available in a shared test environment before identity resolution testing.
- Change advisory board (CAB) slot for the production cutover.

## 8. Assumptions

- The CRM and MDM teams provide named technical contacts within two weeks of approval.
- Existing customer data quality is representative of the sample already profiled.
- Delivery is incremental: integrations are released in stages rather than in one cutover.
- Target date is the end of Q1 next year.

## 9. Risks

- Matching rules are disputed between business units, which delays identity resolution.
- Privacy review finds that merged profiles need extra consent handling.
- Test environment for the CRM to MDM integration is not ready on time.
