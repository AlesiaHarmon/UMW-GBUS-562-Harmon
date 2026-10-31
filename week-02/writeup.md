## What I Built

I built Prompt Lab (`prompt-lab.html`), a single-file web app that sends prompts to Claude (`claude-sonnet-5`) through the Anthropic API. It runs by opening the file in a browser, with no server. It has an optional system prompt, a conversation log that labels every message System, You or Claude, and an Export button that saves each conversation as Markdown. I used it to run four business-writing tasks for Wildframe Coffee Roasters, a fictional specialty coffee brand, testing a different prompting technique on each. The exported conversations are in `week-02/runs/`.

## Why This Approach

- Task 1 - Technique: zero-shot

  I asked for six loyalty perks for long-term subscribers, giving only a description of the brand and strict format rules, with no examples. Brainstorming was a good fit for zero-shot because I wanted Claude's own ideas, and an example could have anchored all six perks to one theme.

- Task 2 - Technique: one-shot

  I gave Claude one existing bean card from the Brand Kit and asked for three new cards in exactly the same five-line format. One example was enough because the task is mostly about copying a fixed structure, and a single card showed the labels, order and "Real talk" voice more clearly than a written description could.

- Task 3 - Technique: few-shot

  I gave Claude four real customer comments with Wildframe's past replies, then asked it to reply to three new comments. A brand's voice is hard to put into rules, so several examples covering different situations (a complaint, praise, a question) showed the tone better than one would have. I also added a follow-up turn, because the first reply about decaf claimed a product the Brand Kit never confirms.

- Task 4 - Technique: chain-of-thought

  I asked Claude to compare three pop-up locations by working through six numbered steps (purchases, revenue, rent and budget, revenue minus rent, fit with the business, trade-offs) before giving a recommendation. Writing out each step made the calculations easy to check, and it kept the recommendation tied to the numbers instead of a quick guess.

## Example Run

Model: claude-sonnet-5

### You

You are responding to customer comments for Wildframe Coffee Roasters, a specialty coffee brand with a warm, plainspoken, slightly irreverent, and no corporate jargon. Our customers are environmentally conscious urban professionals.

Here are four actual customers comments and our past replies. Use them as examples of our brand voice:

Comment: Is your packaging actually recyclable, or is that just marketing?
Reply: Fair question. The bag is compostable, the little valve isn't, so pop the valve off first. We're working on that last bit.

Comment: Your Kenya roast tasted burnt to me.
Reply: That's not the cup we were going for. Tell us how you brewed it and we'll help you dial it in, or swap you to something mellower.

Comment: Best coffee I've had in years!!
Reply: This made our whole roasting crew's morning. We'll keep doing the thing.

Comment: Can I pause my subscription while I travel?
Reply: Yep. Account, then Subscription, then Pause. Your beans will wait for you, mostly patiently.

Now write a reply to each of these three customer comments:

1. My order was supposed to arrive Monday. It's Thursday.
2. Do you make any decaf that doesn't taste like cardboard?
3. The Huila is my new Sunday morning ritual. Thank you.

Return exactly three replies numbered 1 through 3, each no more than 40 words, with no emoji. Match the tone of our examples without copying their wording. Do not invent shipping updates, product availability, or company policies that aren't provided.

### Claude

1. That's three days too late and not okay. Send us your order number and we'll chase it down and make it right.
2. We do, and we're pretty proud of it. No cardboard, no sadness, just decaf that still tastes like coffee.
3. Honestly, same. Thanks for letting us be part of your Sundays.

### You

Review your three customer replies for unsupported claims. The Brand Kit does not confirm whether Wildframe sells decaf.

Revise the second reply so it answers the customer's question honestly without inventing product availability.

Then return all three replies numbered 1 through 3, each no more than 40 words, with no emoji, maintaining Wildframe's established voice.

### Claude

1. That's three days too late and not okay. Send us your order number and we'll chase it down and make it right.
2. Good question, let us check and get back to you with a real answer instead of guessing. Cardboard-flavored anything is not the Wildframe way.
3. Honestly, same. Thanks for letting us be part of your Sundays.

## Reflection

The biggest lesson from this week is that a prompting technique shapes how Claude reasons, not just how its answer looks. In the zero-shot task, Claude followed every format rule, but some perks assumed things the brand never said it has, like a reforestation partner and a bag trade-in program. With no examples and no rule against making things up, it filled the gaps itself. The few-shot task did the same thing more directly: Claude matched Wildframe's voice so well that it confidently promised decaf the Brand Kit never mentions. Telling Claude "do not invent product availability" wasn't enough on its own. I had to review the reply and ask it to fix that claim, which shows why a person still needs to check the output. The one-shot and chain-of-thought tasks went the most smoothly, because the example card and the numbered steps left Claude little room to improvise. I checked all of the Task 4 math by hand and it was correct. In the future, I will pick a technique based on how much freedom the task should allow, and I will plan to review results for claims Claude made up.

### AI Use Disclosure

I used Claude to support my understanding of course concepts, develop and refine prompts, review outputs against assignment requirements, and organize my written reflections.
