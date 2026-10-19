# Meeting Notes: Customer 360 Kickoff Discussion

**Date:** 2026-10-14
**Attendees:** Engineering Lead, Data Engineer, Delivery Engineer, Privacy Lead, Product Owner (Customer Service), Resource Manager
**Synthetic sample for Delivery Twin demos. All names and figures are fictional.**

## Purpose

Agree the approach for the customer 360 request before the resource allocation meeting.

## Discussion

**Scope.** The Product Owner confirmed the first release only needs the contact center to see one profile per
customer. Marketing segmentation and the self-service portal can wait for a later phase.

**Approach.** The Engineering Lead proposed an incremental integration: connect the CRM and the MDM first, then
add identity resolution, then the profile API. A single big-bang cutover was rejected as too risky.

**Data.** The Data Engineer reported about 15 percent suspected duplicate customers in the CRM extract. Matching
rules for households and shared emails are not yet agreed. The Product Owner will arrange a decision with the
regional business leads.

**Privacy.** The Privacy Lead said a privacy review is required before any merged profile is stored, and that
consent management must supply consent flags for every merged profile. The review usually takes three weeks.

**Environments.** The CRM to MDM integration has no shared test environment yet. The Delivery Engineer estimated
two weeks to stand one up once access is approved.

**Capacity.** The Resource Manager can allocate five engineers now. One additional data engineer is possible only
if the date moves out. Two engineers have approved time away in December.

**Cutover.** A change advisory board (CAB) slot is needed for production cutover and these usually book out six
weeks ahead.

## Decisions

- Proceed with incremental integration, contact center first.
- Marketing segmentation and portal redesign are out of scope for this release.
- Target date is the end of Q1 next year, to be confirmed at the resource allocation meeting.

## Action items

- Product Owner: arrange a matching rules decision with the regional business leads.
- Privacy Lead: start the privacy review of identifier matching and storage.
- Delivery Engineer: request access for the shared CRM to MDM test environment.
- Resource Manager: confirm allocations around the December time away.
- Engineering Lead: request an early CAB slot for cutover.
