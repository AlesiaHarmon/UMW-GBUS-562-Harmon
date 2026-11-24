## What I Built

For Week 5 I built a small team of AI agents that reads one supplier email about a purchase order, works out which customer orders it affects, and drafts the replies a person at the company needs to check. I built it first for Brookfield Bicycle Company, and in Part B I made the same code work for a second company, Copperline Coffee Machines, chosen with `--org copperline-coffee`. Everything is in `week-05`:

- `supply_tools.py` and `supply_mcp_server.py`: three plain-Python tools served over MCP: `get_part`, `get_orders_for_part` and `check_coverage`. Coverage is worked out in code, not by the model: orders are filled complete in ship-day order, so a short order holds up the ones after it. The code finds each CSV column by the words in its header, so Copperline's `product`, `units`, `ship_by_day` and `per_unit` work as well as Brookfield's `model`, `qty`, `ship_in_days` and `qty_per_bike`.
- `specialists.py`: four specialists on `claude-sonnet-5`, each reading its system prompt from the organization's `prompts` folder. The supply analyst is an agent that uses the MCP tools (at most 10 tool calls) and returns a JSON brief. The supplier writer and the customer writer each make one model call, and the reviewer answers APPROVE or REVISE, with at most 2 revisions.
- Code checks around the model: the analyst's brief is checked against its own `check_coverage` results (on Email 2 this added order O-2205, which the analyst had left out). The supplier writer gets a sanitized brief with no customer names, order IDs, penalties or internal decisions, plus the number of orders delayed and the fewest and most days late, computed in code and given separately for each option. The customer writer gets only its own order's facts, the days late rather than internal day numbers, and a note when the delay depends on an option nobody has chosen yet. Code then checks every draft for names, wrong delay ranges and language that chooses an option, and marks it for a person when needed.
- `pipeline.py` runs the specialists in a fixed order: analyst, a customer notice for each order at risk, then the supplier reply. `orchestrator.py` gives Claude four tools (`run_supply_analyst`, `draft_supplier_reply`, `draft_customer_notice`, `finish`) and lets it decide which to use, with at most 10 tool calls.
- `offline_tests.py` runs 142 checks for both companies without calling Claude: coverage arithmetic against a separate day-by-day simulation, header detection, customer-name privacy, the computed delay impact, the brief reconciliation, and the MCP server.

No email is ever sent. Both coordinators only save drafts in `week-05/drafts/<org>/` for a person to check. Each final draft is printed once, with "Marked for a person: yes" or "no" after it.

## Why This Approach

Task A: Agent — A leaking espresso machine could be a warranty claim, damage in shipping or a usage problem, so I can't know the steps in advance. One agent that decides which order and warranty tools to check, based on what it finds, and then drafts the reply fits better than a fixed script.

Task B: Team — This job has separate parts that need different skills and rules. Finding the affected machines is data work, customer notices and a regulator report go to different readers with different privacy rules, and a separate reviewer catches mistakes before a person sees them. That's how my reviewer and code checks caught problems this week.

Task C: Pipeline — Every new wholesale order goes through the same stock check, confirmation and review, in the same order. A fixed pipeline is cheaper, predictable and easier to audit, because there's no choice for a model to make about what comes next.

Task D: Orchestrator — Incoming messages differ (a delay, a choice between options, a simple notice), so a coordinator that reads each one and calls only the specialists it needs fits best. On Email 4 my orchestrator used 1,368 input tokens, while the pipeline used 6,536 running every step.

## Example Run

This is the complete saved output of the pipeline on Copperline Email 2, copied unedited from `week-05/runs/copperline-02-pipeline.txt`. Marlow Metalworks offered two shipping options. The run shows the analyst checking each option separately, code adding order O-2205 that the analyst left out, two conditional customer notices ("may ship up to"), and a supplier reply that compares both options without choosing one.

```text
Organization: copperline-coffee
Supplier email: week-05\copperline-coffee\supplier-emails\02-marlow-boiler-options.eml

######################################################################
# STEP 1: supply analyst
######################################################################

=== HANDOFF  coordinator -> analyst: supplier email (copperline-coffee)
From: Victor Lindqvist <v.lindqvist@marlowmetal.example>
To: Purchasing <purchasing@copperlinecoffee.example>
Subject: PO-5520 - BL-12 boilers - please choose a shipping option

Hi,

About PO-5520 for 200 BL-12 boilers: our welding cell is running at half
capacity this month, so we can't ship the whole order on time. We can offer
two options:

Option A: Ship 80 boilers on the scheduled date, and the remaining 120 two
weeks after that.

Option B: Ship all 200 boilers together, six days after the scheduled date.

Please let us know which option you'd like within three business days. If we
don't hear from you by then, we'll go with Option B.

Regards,
Victor Lindqvist
Sales Coordinator, Marlow Metalworks
    [tokens] analyst: 2595 input, 149 output this call (total 2595 input, 149 output in 1 calls)

=== HANDOFF  analyst -> MCP tool get_part: tool call 1 of 10
{
  "part_id": "BL-12"
}
    [result] 331 characters

=== HANDOFF  analyst -> MCP tool get_orders_for_part: tool call 2 of 10
{
  "part_id": "BL-12"
}
    [result] 1732 characters
    [tokens] analyst: 3755 input, 276 output this call (total 6350 input, 425 output in 2 calls)

=== HANDOFF  analyst -> MCP tool check_coverage: tool call 3 of 10
{
  "part_id": "BL-12",
  "arrivals": [
    {
      "qty": 80,
      "arrives_in_days": 7
    },
    {
      "qty": 120,
      "arrives_in_days": 21
    }
  ]
}
    [result] 2229 characters

=== HANDOFF  analyst -> MCP tool check_coverage: tool call 4 of 10
{
  "part_id": "BL-12",
  "arrivals": [
    {
      "qty": 200,
      "arrives_in_days": 13
    }
  ]
}
    [result] 2173 characters
    [tokens] analyst: 6023 input, 999 output this call (total 12373 input, 1424 output in 3 calls)

=== HANDOFF  analyst -> coordinator: full brief (JSON)
{
  "part_id": "BL-12",
  "what_changed": "PO-5520's 200 boilers, originally due day 7, will now ship either as 80 on day 7 plus 120 on day 21 (Option A) or all 200 together on day 13 (Option B, the supplier's default if no response is given).",
  "orders_at_risk": [
    {
      "order_id": "O-2203",
      "customer": "Elm State University Dining",
      "units_short": 40,
      "ship_day": 12,
      "ready_day": 13,
      "late_penalty": true
    }
  ],
  "options": [
    {
      "name": "Option A: 80 on day 7, 120 on day 21",
      "order_ids": [
        "O-2205"
      ]
    },
    {
      "name": "Option B: all 200 on day 13 (supplier's default)",
      "order_ids": [
        "O-2203"
      ]
    }
  ],
  "decisions_for_a_person": [
    "Choose between Option A (risks order O-2205, 5 units short, ready day 21, 4 days late, late penalty applies) and Option B (risks order O-2203, 40 units short, ready day 13, 1 day late, late penalty applies).",
    "Respond to Marlow Metalworks within three business days, otherwise Option B will be applied by default."
  ],
  "supplier_questions": [
    "Please let us know which shipping option (A or B) you'd like within three business days."
  ]
}

=== HANDOFF  code -> coordinator: brief checked against 2 check_coverage result(s)
{
  "corrections": [
    "O-2205: added to orders_at_risk from check_coverage"
  ],
  "problems": "none"
}

=== HANDOFF  code -> coordinator: reconciled brief (JSON)
{
  "part_id": "BL-12",
  "what_changed": "PO-5520's 200 boilers, originally due day 7, will now ship either as 80 on day 7 plus 120 on day 21 (Option A) or all 200 together on day 13 (Option B, the supplier's default if no response is given).",
  "orders_at_risk": [
    {
      "order_id": "O-2203",
      "customer": "Elm State University Dining",
      "units_short": 40,
      "ship_day": 12,
      "ready_day": 13,
      "late_penalty": true,
      "by_option": {
        "Option B: all 200 on day 13 (supplier's default)": {
          "units_short": 40,
          "ready_day": 13,
          "days_late": 1
        }
      }
    },
    {
      "order_id": "O-2205",
      "customer": "Tall Pines Coffee House",
      "units_short": 5,
      "ship_day": 17,
      "ready_day": 21,
      "late_penalty": true,
      "by_option": {
        "Option A: 80 on day 7, 120 on day 21": {
          "units_short": 5,
          "ready_day": 21,
          "days_late": 4
        }
      }
    }
  ],
  "options": [
    {
      "name": "Option A: 80 on day 7, 120 on day 21",
      "order_ids": [
        "O-2205"
      ]
    },
    {
      "name": "Option B: all 200 on day 13 (supplier's default)",
      "order_ids": [
        "O-2203"
      ]
    }
  ],
  "decisions_for_a_person": [
    "Choose between Option A (risks order O-2205, 5 units short, ready day 21, 4 days late, late penalty applies) and Option B (risks order O-2203, 40 units short, ready day 13, 1 day late, late penalty applies).",
    "Respond to Marlow Metalworks within three business days, otherwise Option B will be applied by default."
  ],
  "supplier_questions": [
    "Please let us know which shipping option (A or B) you'd like within three business days."
  ],
  "reconciliation": {
    "check_coverage_results": 2,
    "corrections": [
      "O-2205: added to orders_at_risk from check_coverage"
    ],
    "problems": []
  }
}

######################################################################
# STEP 2: customer notices for 2 order(s) at risk
######################################################################

=== HANDOFF  coordinator -> customer writer: facts for order O-2203
Order facts:
{
  "order_id": "O-2203",
  "customer": "Elm State University Dining",
  "product": "Cafe One",
  "quantity": 50,
  "days_late": 1,
  "cause": "a delay in a part from one of our suppliers",
  "note": "The delay is NOT certain: it depends on a decision we have not made yet. Say the order may ship up to the number of days late shown, never that it will, and that we will confirm the ship date.",
  "delay_is_certain": false
}
    [tokens] customer writer: 392 input, 205 output this call (total 392 input, 205 output in 1 calls)

=== HANDOFF  customer writer -> coordinator: customer draft for O-2203
(111 words; the final draft is printed after the review)

=== HANDOFF  coordinator -> reviewer: customer draft and its facts
Facts:
{
  "order_id": "O-2203",
  "customer": "Elm State University Dining",
  "product": "Cafe One",
  "quantity": 50,
  "days_late": 1,
  "cause": "a delay in a part from one of our suppliers",
  "note": "The delay is NOT certain: it depends on a decision we have not made yet. Say the order may ship up to the number of days late shown, never that it will, and that we will confirm the ship date.",
  "delay_is_certain": false
}
Draft: the latest customer draft (111 words; the final draft is printed after the review)
    [tokens] reviewer: 708 input, 194 output this call (total 708 input, 194 output in 1 calls)

=== HANDOFF  reviewer -> coordinator: verdict APPROVE
(no reasons given)

FINAL DRAFT TO CUSTOMER O-2203
Subject: Update on Your Order O-2203

Dear Elm State University Dining,

We're writing about your order O-2203 for 50 units of the Cafe One.

We apologize — a delay in a part from one of our suppliers may affect this order's shipping timeline. At this time, we want to let you know the order may ship up to 1 day later than originally planned. This is not yet certain, as it depends on a decision we have not finalized.

We will confirm the exact ship date as soon as we have more information, and we appreciate your patience in the meantime.

Thank you for your understanding.

Customer Service, Copperline Coffee Machines
Marked for a person: yes

=== SAVED  week-05/drafts/copperline-coffee/20261123-212310_customer_O-2203_NEEDS-PERSON.txt
    - CONDITIONAL NOTICE: the supplier offered options and none is chosen yet; this delay depends on the option chosen.
    - Order has a late penalty.

=== HANDOFF  coordinator -> customer writer: facts for order O-2205
Order facts:
{
  "order_id": "O-2205",
  "customer": "Tall Pines Coffee House",
  "product": "Cafe One",
  "quantity": 25,
  "days_late": 4,
  "cause": "a delay in a part from one of our suppliers",
  "note": "The delay is NOT certain: it depends on a decision we have not made yet. Say the order may ship up to the number of days late shown, never that it will, and that we will confirm the ship date.",
  "delay_is_certain": false
}
    [tokens] customer writer: 395 input, 196 output this call (total 787 input, 401 output in 2 calls)

=== HANDOFF  customer writer -> coordinator: customer draft for O-2205
(103 words; the final draft is printed after the review)

=== HANDOFF  coordinator -> reviewer: customer draft and its facts
Facts:
{
  "order_id": "O-2205",
  "customer": "Tall Pines Coffee House",
  "product": "Cafe One",
  "quantity": 25,
  "days_late": 4,
  "cause": "a delay in a part from one of our suppliers",
  "note": "The delay is NOT certain: it depends on a decision we have not made yet. Say the order may ship up to the number of days late shown, never that it will, and that we will confirm the ship date.",
  "delay_is_certain": false
}
Draft: the latest customer draft (103 words; the final draft is printed after the review)
    [tokens] reviewer: 702 input, 7 output this call (total 1410 input, 201 output in 2 calls)

=== HANDOFF  reviewer -> coordinator: verdict APPROVE
(no reasons given)

FINAL DRAFT TO CUSTOMER O-2205
Subject: Update on Your Order O-2205

Dear Tall Pines Coffee House,

We're writing about your order O-2205 for 25 units of the Cafe One.

Due to a delay in a part from one of our suppliers, your order may ship up to 4 days later than originally planned. We have not yet made a final decision on the timing, and we will confirm the exact ship date with you as soon as it is set.

We apologize for the uncertainty this may cause.

Thank you for your patience, and we'll be in touch soon with a confirmed date.

Customer Service, Copperline Coffee Machines
Marked for a person: yes

=== SAVED  week-05/drafts/copperline-coffee/20261123-212313_customer_O-2205_NEEDS-PERSON.txt
    - CONDITIONAL NOTICE: the supplier offered options and none is chosen yet; this delay depends on the option chosen.
    - Order has a late penalty.

######################################################################
# STEP 3: reply to the supplier
######################################################################

=== HANDOFF  coordinator -> supplier writer: email and sanitized brief (no customer names, order IDs, penalties or internal decisions; impact computed in code)
Supplier email:
From: Victor Lindqvist <v.lindqvist@marlowmetal.example>
To: Purchasing <purchasing@copperlinecoffee.example>
Subject: PO-5520 - BL-12 boilers - please choose a shipping option

Hi,

About PO-5520 for 200 BL-12 boilers: our welding cell is running at half
capacity this month, so we can't ship the whole order on time. We can offer
two options:

Option A: Ship 80 boilers on the scheduled date, and the remaining 120 two
weeks after that.

Option B: Ship all 200 boilers together, six days after the scheduled date.

Please let us know which option you'd like within three business days. If we
don't hear from you by then, we'll go with Option B.

Regards,
Victor Lindqvist
Sales Coordinator, Marlow Metalworks


Supply analyst's brief:
{
  "part_id": "BL-12",
  "what_changed": "PO-5520's 200 boilers, originally due day 7, will now ship either as 80 on day 7 plus 120 on day 21 (Option A) or all 200 together on day 13 (Option B, the supplier's default if no response is given).",
  "impact": "Depends on the option chosen; see each option's impact.",
  "impact_note": "Worked out in code from the analyst's order data. State each option's impact separately with these numbers (how many orders it delays, and the fewest to the most days late); never combine options into one figure, and don't work out your own.",
  "options": [
    {
      "name": "Option A: 80 on day 7, 120 on day 21",
      "orders_delayed": 1,
      "days_late_fewest": 4,
      "days_late_most": 4
    },
    {
      "name": "Option B: all 200 on day 13 (supplier's default)",
      "orders_delayed": 1,
      "days_late_fewest": 1,
      "days_late_most": 1
    }
  ],
  "supplier_questions": [
    "Please let us know which shipping option (A or B) you'd like within three business days."
  ]
}
    [tokens] supplier writer: 980 input, 238 output this call (total 980 input, 238 output in 1 calls)

=== HANDOFF  supplier writer -> coordinator: supplier draft
(112 words; the final draft is printed after the review)

=== HANDOFF  coordinator -> reviewer: supplier draft and its facts
Facts:
{
  "supplier_email": "From: Victor Lindqvist <v.lindqvist@marlowmetal.example>\nTo: Purchasing <purchasing@copperlinecoffee.example>\nSubject: PO-5520 - BL-12 boilers - please choose a shipping option\n\nHi,\n\nAbout PO-5520 for 200 BL-12 boilers: our welding cell is running at half\ncapacity this month, so we can't ship the whole order on time. We can offer\ntwo options:\n\nOption A: Ship 80 boilers on the scheduled date, and the remaining 120 two\nweeks after that.\n\nOption B: Ship all 200 boilers together, six days after the scheduled date.\n\nPlease let us know which option you'd like within three business days. If we\ndon't hear from you by then, we'll go with Option B.\n\nRegards,\nVictor Lindqvist\nSales Coordinator, Marlow Metalworks\n",
  "brief": {
    "part_id": "BL-12",
    "what_changed": "PO-5520's 200 boilers, originally due day 7, will now ship either as 80 on day 7 plus 120 on day 21 (Option A) or all 200 together on day 13 (Option B, the supplier's default if no response is given).",
    "impact": "Depends on the option chosen; see each option's impact.",
    "impact_note": "Worked out in code from the analyst's order data. State each option's impact separately with these numbers (how many orders it delays, and the fewest to the most days late); never combine options into one figure, and don't work out your own.",
    "options": [
      {
        "name": "Option A: 80 on day 7, 120 on day 21",
        "orders_delayed": 1,
        "days_late_fewest": 4,
        "days_late_most": 4
      },
      {
        "name": "Option B: all 200 on day 13 (supplier's default)",
        "orders_delayed": 1,
        "days_late_fewest": 1,
        "days_late_most": 1
      }
    ],
    "supplier_questions": [
      "Please let us know which shipping option (A or B) you'd like within three business days."
    ]
  }
}
Draft: the latest supplier draft (112 words; the final draft is printed after the review)
    [tokens] reviewer: 1313 input, 7 output this call (total 2723 input, 208 output in 3 calls)

=== HANDOFF  reviewer -> coordinator: verdict APPROVE
(no reasons given)

FINAL DRAFT TO SUPPLIER
Subject: RE: PO-5520 - BL-12 boilers - please choose a shipping option

Hi Victor,

Thanks for flagging this. To confirm our understanding of the two options for PO-5520 (200 BL-12 boilers, originally due day 7):

- Option A (80 on day 7, 120 on day 21): this delays 1 order by 4 days.
- Option B (all 200 on day 13): this delays 1 order by 1 day.

We will confirm our choice within the three business days you've requested.

In the meantime, is there any way to ship more units sooner than either option allows, to reduce the delay further? Any improvement there would help us.

Regards,
Purchasing, Copperline Coffee Machines
Marked for a person: yes

=== SAVED  week-05/drafts/copperline-coffee/20261123-212317_supplier_BL-12_NEEDS-PERSON.txt
    - The supplier offered options; a person must choose one before this is sent.
    - Affects orders with a late penalty: O-2203, O-2205

======================================================================
SUMMARY
======================================================================

Drafts saved: 3
  - 20261123-212310_customer_O-2203_NEEDS-PERSON.txt  (customer, order O-2203; reviewer APPROVE, 0 revision(s))
  - 20261123-212313_customer_O-2205_NEEDS-PERSON.txt  (customer, order O-2205; reviewer APPROVE, 0 revision(s))
  - 20261123-212317_supplier_BL-12_NEEDS-PERSON.txt  (supplier; reviewer APPROVE, 0 revision(s))

Drafts marked for a person: 3
  - 20261123-212310_customer_O-2203_NEEDS-PERSON.txt
      CONDITIONAL NOTICE: the supplier offered options and none is chosen yet; this delay depends on the option chosen.
      Order has a late penalty.
  - 20261123-212313_customer_O-2205_NEEDS-PERSON.txt
      CONDITIONAL NOTICE: the supplier offered options and none is chosen yet; this delay depends on the option chosen.
      Order has a late penalty.
  - 20261123-212317_supplier_BL-12_NEEDS-PERSON.txt
      The supplier offered options; a person must choose one before this is sent.
      Affects orders with a late penalty: O-2203, O-2205

Decisions for a person: 2
  - Choose between Option A (risks order O-2205, 5 units short, ready day 21, 4 days late, late penalty applies) and Option B (risks order O-2203, 40 units short, ready day 13, 1 day late, late penalty applies).
  - Respond to Marlow Metalworks within three business days, otherwise Option B will be applied by default.

=== TOKENS USED BY EACH SPECIALIST
  analyst           3 calls    12373 input    1424 output
  customer writer   2 calls      787 input     401 output
  reviewer          3 calls     2723 input     208 output
  supplier writer   1 calls      980 input     238 output
  all                          16863 input    2271 output
```

## Reflection

Email 1 reading: Matches — Diane wrote "The other 160 will follow 15 days later." The pipeline's what_changed says "the remaining 160 will now arrive on day 19 (15 days later)". That is the scheduled day 4 plus 15 days, so day 19 is right.

Email 1 customer names: No — Diane asked for each customer's name and quantity, but the pipeline's supplier reply said "We're not able to share customer names or order details" and gave only the overall impact: 3 orders, 2 to 7 days late. The supplier writer never received customer names or order IDs, and code checked the draft for them before saving it.

Email 4 orchestrator: None — its first and only tool call was finish ("no reply or customer notice needed", no decision needed), so it called no specialist. The pipeline still ran its fixed steps. The analyst called get_part and get_orders_for_part (no check_coverage, since nothing changed), the customer notices were skipped, and the supplier writer answered NO REPLY NEEDED, so the reviewer was not called. Its summary lists that draft as "reviewer APPROVE" only because the code treats NO REPLY NEEDED as nothing to review. The pipeline used 6,536 input tokens; the orchestrator used 1,368.

The two coordinators made different decisions on the same emails:

| Email | Pipeline (fixed order) | Orchestrator (Claude decides) |
|---|---|---|
| 1, Danforth pumps | 3 customer notices and a supplier reply; 16,239 input / 2,795 output tokens | The same four drafts, supplier reply first; a person must decide; 23,129 / 3,326 |
| 2, Marlow options | 2 conditional notices and a supplier reply comparing both options, all marked for a person; 16,863 / 2,271 | Only the analyst, then finish: it left the choice of option to a person before drafting anything; 16,065 / 2,928 |
| 3, Ashgrove 3 days late | A supplier reply saying no orders are delayed, no notices; 11,374 / 1,142 | The same: analyst, then supplier reply; 16,385 / 2,159 |
| 4, Pelham closure | NO REPLY NEEDED, no notices; 6,536 / 673 | finish only; 1,368 / 279 |

In total the pipeline used 51,012 input and 6,881 output tokens, and the orchestrator 56,947 and 8,692. The orchestrator saves the most on emails that need nothing, but its own calls cost extra when it uses every specialist, as on Email 1. Neither coordinator chose an option for Copperline, and neither sent anything. The Email 3 orchestrator summary says the reply was "approved for sending", but that only means the reviewer approved the draft. It was saved for a person, not sent.

Testing on real runs found problems the model did not catch on its own, and each was fixed in general code, then rerun. The first Email 1 supplier reply said the delays ranged "from 4 to 7 days" when the real range was 2 to 7, so code now computes the range and marks any draft that states a different one. On Email 2 the analyst left O-2205 out of its orders at risk, so code now checks the brief against every check_coverage result for every option. The supplier reply then combined both options into "2 orders, 1 to 4 days", which describes no option that can actually be chosen, so code now gives each option's impact separately. One orchestrator summary also had tool-call text mixed into it, so the finish call is now checked before it's accepted. My main lesson is that the model writes well but should not be trusted with arithmetic or privacy on its own. Those are safest computed and checked in code, with a person making the final decision.
