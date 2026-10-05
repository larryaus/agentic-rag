# Customer Support On-Call Runbook

## 1. Scope of Service

The customer support team handles product usage questions, account and permission issues, billing issues, failed file uploads, API errors, and urgent tickets from enterprise customers.

Issues outside the scope of front-line support include:
- Faults in the customer's own network or devices;
- Interface changes in third-party systems;
- Custom development requests not covered by a signed contract;
- Non-technical approvals such as legal, procurement, or changes to the name on an invoice.

When an issue is out of scope, explain the limits of what support can help with and hand it over to the responsible team.

## 2. Ticket Priorities

### P0: Major incident

A ticket is P0 if any one of these applies:
- The production environment is widely unavailable;
- Multiple enterprise customers cannot sign in or search documents;
- Accidental data deletion, unauthorized access, or a suspected security incident;
- The API error rate for paying customers stays above 30% for 10 consecutive minutes.

Response requirements: acknowledge within 5 minutes, escalate to the on-call engineer within 15 minutes, and give the first status update within 30 minutes.

### P1: High priority

Typical cases:
- A single enterprise customer cannot use a core feature;
- File upload or parsing keeps failing;
- A bill amount is clearly wrong;
- A customer asks to restore a document deleted within the last 7 days.

Response requirements: acknowledge within 30 minutes, and provide a resolution plan or a clear next step within 4 hours.

### P2: Normal

Typical cases:
- Questions about how to use a feature;
- Failed member invitations;
- Changing the company name, contact person or notification email;
- Questions about plan quotas, API rate limits or data export steps.

Response requirement: reply within 1 working day.

## 3. Standard Handling Process

1. Review the ticket source, customer tier, communication history and system events from the last 24 hours.
2. Reproduce the issue or gather evidence, including the request ID, account email, time range, file name, browser version and a screenshot of the error.
3. Decide the priority and record the reasoning in the ticket.
4. Reply to the customer using the standard templates, and avoid promising a fix time that has not been confirmed.
5. When engineering needs to step in, attach minimal reproduction steps, links to logs and the scope of impact.
6. Once the issue is resolved, confirm that the customer can use the product normally before closing the ticket.

## 4. Common Reply Templates

### File upload failure

Hello, we have received your report. Please first confirm that the file is a PDF, Markdown, TXT or HTML file and that its size does not exceed the limit for your current plan. If it still fails, please send us the upload time, file name, a screenshot of the error and the request ID shown on the page, and we will investigate further.

### API rate limiting

Hello, your requests have triggered the rate limit for your plan. The Pro plan is limited by default to 60 RPM and 10,000 tokens per minute; the Enterprise plan can be configured separately. If your workload has peak demand, please send us the expected concurrency, the time window and the use case, and we can help assess options for raising the limit.

### Data deletion and recovery

Hello, a deleted account or document enters a 30-day grace period. Please tell us the type of data to restore, when it was deleted, the account that performed the deletion and the workspace name. We will submit a restore request once we have verified administrator permission.

## 5. Escalation Rules

- Issues involving security, privacy or unauthorized access are escalated immediately to the security lead.
- Issues involving production error rates, service unavailability or a backlog in the task queue are escalated to the on-call engineer.
- Issues involving contracts, refunds, bank transfers or invoice disputes are escalated to business operations.
- Issues involving the media, regulators or legal letters are escalated to legal and company leadership.

An escalation must include: the customer name, a summary of the issue, the scope of impact, when it first occurred, the actions already taken, and the support being requested.

## 6. Closing Criteria

A ticket can be closed when any of these applies:
- The customer has explicitly confirmed that the issue is resolved;
- No further information has been received from the customer for 3 consecutive working days;
- The issue is out of scope for support and the customer has been told the correct channel;
- The engineering team has confirmed that the defect is fixed and the customer has been given steps to verify it.

Before closing a ticket, check that the internal notes are complete, so that investigation findings and follow-up improvements are not lost.
