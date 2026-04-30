# Salesforce Demo Test Data

Create these records in your Salesforce demo org before the demo. All company names are fictional.

---

## Accounts

| Account Name | Industry | Type | Phone | Website |
|-------------|----------|------|-------|---------|
| ACME Healthcare | Healthcare | Customer - Direct | (555) 100-2000 | www.acmehealthcare.example.com |
| Zephyr Software | Technology | Prospect | (555) 200-3000 | www.zephyrsoftware.example.com |
| Brightpath Financial | Financial Services | Prospect | (555) 300-4000 | www.brightpathfinancial.example.com |

## Contacts

| Name | Account | Title | Email | Phone |
|------|---------|-------|-------|-------|
| James Wilson | ACME Healthcare | Chief Technology Officer | jwilson@acmehealthcare.example.com | (555) 100-2001 |
| Sarah Mitchell | ACME Healthcare | VP Engineering | smitchell@acmehealthcare.example.com | (555) 100-2002 |
| Lisa Park | Zephyr Software | Director of IT | lpark@zephyrsoftware.example.com | (555) 200-3001 |
| David Okafor | Brightpath Financial | Chief Information Officer | dokafor@brightpathfinancial.example.com | (555) 300-4001 |

## Opportunities

### ACME Healthcare — Patient Records Modernization

| Field | Value |
|-------|-------|
| **Opportunity Name** | ACME Healthcare - Patient Records Modernization |
| **Account** | ACME Healthcare |
| **Amount** | $1,800,000 |
| **Close Date** | 2026-06-30 |
| **Stage** | Negotiation/Review |
| **Probability** | 75% |
| **Primary Contact** | James Wilson |
| **Next Step** | Finalize SOW and pricing with legal |
| **Description** | Modernize patient records platform. Requires HIPAA compliance and Epic systems integration. CTO James Wilson is executive sponsor. Three meetings held to date. |

### Zephyr Software — Cloud Migration

| Field | Value |
|-------|-------|
| **Opportunity Name** | Zephyr Software - Cloud Migration |
| **Account** | Zephyr Software |
| **Amount** | $750,000 |
| **Close Date** | 2026-08-15 |
| **Stage** | Proposal/Price Quote |
| **Probability** | 50% |
| **Primary Contact** | Lisa Park |
| **Next Step** | Submit final proposal by end of week |
| **Description** | Full cloud migration from on-premise data center. 200+ servers, 50 applications. Lisa Park leading evaluation. Proposal submitted, awaiting review. |

### Brightpath Financial — Data Analytics Platform

| Field | Value |
|-------|-------|
| **Opportunity Name** | Brightpath Financial - Data Analytics Platform |
| **Account** | Brightpath Financial |
| **Amount** | $3,200,000 |
| **Close Date** | 2026-09-30 |
| **Stage** | Qualification |
| **Probability** | 25% |
| **Primary Contact** | David Okafor |
| **Next Step** | Schedule technical deep-dive with their analytics team |
| **Description** | Build enterprise data analytics platform for regulatory reporting and risk modeling. Early-stage discovery. CIO David Okafor interested but needs board approval for budget. |

---

## Quick Setup via Salesforce Data Import

You can create these records using Salesforce's Data Import Wizard:

1. **Setup > Data Import Wizard**
2. Import Accounts first, then Contacts (linking by Account Name), then Opportunities
3. Or create them manually via the Salesforce UI — there are only 3 accounts, 4 contacts, and 3 opportunities

## Verification

After creating the records, test the MCP integration:

1. Start a test meeting in LMA
2. In the chat, type: "Look up ACME Healthcare in Salesforce"
3. Verify the response includes: Account name, Opportunity name, Amount ($1,800,000), Stage (Negotiation), Contact (James Wilson)
4. Also test: "What opportunities are in the proposal stage?" — should return Zephyr Software
