# Відповідь Дімі (NewsCatcher)

---

> Hi Dima,
>
> Thanks — this is the most useful reply we've had, and the fine-tuning point is the one we'd been worrying about ourselves.
>
> **What we're building.** An editorial assistant for the Office of the President of the Kyiv School of Economics — Tymofiy Mylovanov's office. He writes on the economy, Russia's war, sanctions and international security for a public audience on X and Facebook.
>
> Right now an editor reads the day's news, decides what's worth commenting on, and drafts a post in his voice. We're automating everything up to the draft: collect what was published, mark what's relevant and why, pull out the key claim and numbers, write a draft. A human still approves or rejects every one. Nothing publishes on its own, and every post links back to the source.
>
> **Volume.** Smaller than you're used to, so let me be upfront. Our editorial database has 2,868 articles since March across 50 domains — about 530 a month. We can retrieve roughly 300 of those ourselves; the other **230 or so a month** is where you'd actually help. Call it 200–500 full-text fetches monthly to start, plus a few thousand search calls if we move discovery to you as well.
>
> **On fine-tuning.** You're right that it needs its own look, and I'd rather not have it surface post-contract either. A suggestion: start with runtime context under your standard terms. Our MVP genuinely doesn't need more — we already have 2,605 full-text articles our editors collected by hand over six months, which is enough to build and evaluate on. Demo is 25 September. Then treat training scope as a separate conversation once you've had time to work out what's possible. If the answer turns out to be no, that's still useful — we'd design around it rather than find out later.
>
> **One thing you already do better.** Our single biggest source is `united24media.com` — 385 articles, more than Reuters. You have it; Perigon and NewsAPI.ai don't. What you don't have is `pravda.com.ua`, which returned nothing on either test article. How complete is your Ukrainian coverage, and can we ask for specific domains to be added?
>
> Booking a slot now — Ukrainian works better for me too.
>
> Andrii

---

**Що змінилось:** без заголовків і таблиць, коротше вдвічі, тон розмовний. Чотири його питання відповідені, але не списком.

**Головне лишилось:** чесний обсяг (малий — і ми це кажемо самі), пропозиція розділити ліцензію на два етапи, прохання про `united24media`.

Якщо не хочемо називати Тимофія — прибрати пів речення в першому абзаці.
