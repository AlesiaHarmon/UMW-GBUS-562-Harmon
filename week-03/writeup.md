## What I Built

I built a retrieval-augmented generation (RAG) tool using Python, Chroma, and Claude to answer questions based on an organization's internal documents. I adapted the original Copperline Manufacturing tool to work with Larkfield Credit Union's policies by adding document-folder selection and separate vector databases. I also added the ability to save retrieved chunks and answers for evaluation. I completed seven runs, including the before-and-after tests for Question 1 and the five additional questions. Questions 1–4 were run before a retrieval enhancement and retrieved the four most relevant chunks. Questions 5–6 were run after it and retrieved six chunks: the four most relevant chunks plus two related sections from the top-ranked document, so that conditions and exceptions in the same policy could be included. I also updated Claude's instructions so that, when answering a question, it includes any relevant conditions, exceptions, cutoff times, and limits stated in the sources.

## Why This Approach

Chunking method: structure

Makes sense on its own: Yes, the chunks retain their document titles, section headings, and relevant policy language. For example, the overdraft-fee chunk contains the fee amount, daily limit, and applicable exceptions, allowing the information to be understood independently.

Label and source: Both, each chunk begins with the document title and section heading, and its source filename is stored with the chunk. I verified this through the retrieved results and citations.

Short enough: Yes, the chunks fit within the embedding model's 256-token limit. The longest Larkfield chunk contains 128 words and 171 tokens, while the longest Copperline chunk contains 147 words and 205 tokens.

## Example Run

```text
Question: A member deposited a $2,000 paycheck at a branch today. How much of it can they use on the next business day?
Documents: week-03/larkfield-docs
Model: claude-sonnet-5

Rank 1 | similarity 0.584 | source: funds-availability-policy.html
Funds Availability Policy > Check deposits
About this document: Version 3 (current). Owner: Deposit Operations.
For checks deposited at a branch or a Larkfield ATM, the first $275 of the day's check deposits is available on the next business day. The rest of the day's check deposits is available on the second business day after the day of deposit.

Rank 2 | similarity 0.538 | source: funds-availability-policy.html
Funds Availability Policy > Mobile check deposits
About this document: Version 3 (current). Owner: Deposit Operations.
Checks deposited with the Larkfield mobile app follow the same availability as checks deposited at a branch. A member can deposit up to $5,000 per business day through the mobile app. Members should keep the paper check for 30 days after a mobile deposit, then destroy it.

Rank 3 | similarity 0.445 | source: consumer-fee-schedule.html
Consumer Fee Schedule > Official checks and money orders
About this document: Version 8 (current). Replaces Version 7. Owner: Member Services.
A cashier's check costs $8. A money order costs $3. Members can buy up to five money orders in one day.

Rank 4 | similarity 0.440 | source: funds-availability-policy.html
Funds Availability Policy > Business days and cutoff times
About this document: Version 3 (current). Owner: Deposit Operations.
A business day is Monday through Friday, except federal holidays. A deposit made before 5 p.m. on a business day is received that day. A deposit made after 5 p.m., or on a weekend or holiday, is received on the next business day.

Rank 5 | similarity 0.432 | source: funds-availability-policy.html (related section from the rank 1 document)
Funds Availability Policy > Longer holds
About this document: Version 3 (current). Owner: Deposit Operations.
Larkfield may hold check deposits for up to seven business days when the day's check deposits total more than $6,000, when the account has been open fewer than 30 days, when the account has been overdrawn repeatedly in the last six months, or when there is reason to doubt the check will be paid. The member is told about a longer hold when the deposit is made, or by mail if the hold is placed later.

Rank 6 | similarity 0.422 | source: funds-availability-policy.html (related section from the rank 1 document)
Funds Availability Policy > Cash, direct deposits, and wire transfers
About this document: Version 3 (current). Owner: Deposit Operations.
Cash deposited at a branch, direct deposits such as payroll, and incoming wire transfers are available on the business day they are received.

ANSWER
Based on the sources, this depends on how the deposit is received and on a few conditions:

- If it's deposited as a **check** at a branch (rather than electronically as a direct deposit), the first **$275** is available on the next business day, with the remaining $1,725 available on the second business day after deposit [funds-availability-policy.html].
- This assumes the deposit is made before 5 p.m. on a business day; otherwise it's treated as received the next business day [funds-availability-policy.html].
- A longer hold (up to seven business days) could apply instead if the account has been open fewer than 30 days, has had repeated overdrafts in the last six months, or there's reason to doubt the check will be paid — the sources don't say whether any of these apply here [funds-availability-policy.html].
- If the $2,000 was instead a **direct deposit** (e.g., payroll sent electronically) rather than a check brought to the branch, the full amount would be available the same business day it was received [funds-availability-policy.html].

The sources don't specify which type of deposit this is, so the exact answer depends on that detail.
```

The answer correctly includes the 5 p.m. cutoff and the longer-hold conditions, but it over-hedges about the deposit type: the question says the paycheck was deposited at a branch, which makes it a check deposit, so the direct-deposit bullet and the final sentence were unnecessary.

## Reflection

What was wrong: The document consumer-fee-schedule-v7.html was an outdated version of Larkfield's fee schedule, but it still identified itself as current. The retrieved chunks for Question 1 included both Version 7 and Version 8. Although Claude identified the correct $29 overdraft fee from Version 8, the conflicting document introduced uncertainty because Version 7 listed a $35 fee.

How I fixed it: Moved consumer-fee-schedule-v7.html from the active Larkfield document folder into a separate archive folder. I rebuilt the Chroma database afterward and verified that the outdated document no longer appeared in retrieval results.

Prevention: Archive process. Archiving superseded versions would help Larkfield prevent outdated policies from remaining in its active document collection. When a new version becomes effective, the prior version should be archived and the retrieval database updated so representatives receive information from current policies.

Question 6: Declined, because the retrieved chunks concerned share-certificate dividend rates, checking fees, and stop payments rather than mortgage lending. The tool recognized that the available documents did not establish a current 30-year fixed mortgage rate and avoided inventing one.
