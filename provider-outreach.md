# Звернення до провайдерів: куди писати і що писати

**Нічого не надіслано.** Це заготовки — надсилає людина, з робочої пошти.

---

## 1. Куди писати

| Провайдер | Точка входу | Статус |
|---|---|---|
| **Dow Jones / Factiva** | `dowjones.com/professional/factiva/` → Request a Demo. Для API окремо: Dow Jones Developer Platform, продукт **Factiva Retrieval API** | сайт не віддався автоматично — лінк підтвердити вручну |
| **LexisNexis Nexis Data+** | `lexisnexis.com/en-us/products/nexis-data-plus/` → Contact us / Request consultation | форма на сторінці продукту |
| **Opoint** | `opoint.com/products/api-solutions` → «Request a data walkthrough» та «Talk to a specialist» унизу сторінки | ✅ форми перевірені, публічної пошти немає |
| **Webz.io** | `webz.io` → Contact / Get a demo | форма |
| **Perigon** | `perigon.io` → Contact sales. Є безкоштовний тариф (150 запитів/міс) і self-serve від $250–550/міс | почати з безкоштовного ключа, без листування |
| **NewsCatcher** | `newscatcherapi.com` → Contact | є публічний прайс, 7-денний тріал |
| **NewsAPI.ai** | `newsapi.ai/plans` → реєстрація дає 2 000 токенів безкоштовно | почати без листування |
| **Aylien (Quantexa)** | `aylien.com/product/news-api` → 14-денний тріал | форма |

**Порядок дій.** Perigon, NewsCatcher і NewsAPI.ai мають самообслуговування — там швидше зареєструватись і прогнати приймальний тест самому, ніж чекати відповіді продажників. Листи потрібні для Factiva, Nexis Data+, Opoint, Webz.io — тобто саме там, де ліцензований повний текст.

---

## 2. Коротке повідомлення для веб-форми

Більшість форм мають ліміт символів. Це вкладається.

> **Subject:** Full-text news API for an editorial AI assistant — coverage & licensing questions
>
> Hello,
>
> I'm the data architect at the Office of the President of the Kyiv School of Economics — the office of Tymofiy Mylovanov. We're building an internal editorial assistant: it reads news articles, extracts the key claim and facts, and drafts a social media post that always links back to the source. A human editor reviews every draft before publication.
>
> **How we use the text — two scopes, both needed.**
>
> 1. **Runtime grounding.** The article goes into the model's context at generation time; the output is original short-form commentary with attribution and a link back. Nothing is republished.
> 2. **Model training.** We are building a dataset of article-to-post pairs and will fine-tune on it, so article text is the input side of our training data. The style itself is learned from our own published posts, never from yours.
>
> Please quote both scopes, or tell us if scope 2 is not available.
>
> Three questions before we go further:
>
> 1. Do you return the **full article body** (not a snippet) for: nytimes.com, ft.com, economist.com, telegraph.co.uk, thetimes.com, foreignaffairs.com, bloomberg.com, wsj.com, washingtonpost.com? Per-publisher answers would help.
> 2. Does your licence permit passing that text to an LLM to generate derivative summaries with attribution?
> 3. How far back does the archive go **with full body text**?
>
> We have a 24-article test set from our own editorial history and can validate coverage quickly if you provide trial access.
>
> Volume: roughly 300–500 articles/day ingested, ~10 posts/day published.
>
> Thanks,
> [Ім'я] · [посада] · [пошта]

---

## 3. Повний лист (коли є адреса або після першої відповіді)

> **Subject:** RFI — licensed full-text news API for editorial AI (KSE Office of the President)
>
> Hello [Ім'я],
>
> I'm the data architect at the Office of the President of the Kyiv School of Economics, led by Tymofiy Mylovanov. We're selecting a news data provider and would like to check fit before a call.
>
> **What we're building.** An internal editorial assistant. It ingests news articles, marks which are relevant to our principal's areas (economy, Russia's war against Ukraine, international security), extracts the main claim and supporting facts, and drafts a social post. Every draft is reviewed by a human editor before publication, and every published post links to the original article.
>
> **How we use the text — this matters for licensing.** Two scopes. At runtime the article body goes into the model's context and the output is short original commentary with attribution and a link. Separately, we are assembling a dataset of article-to-post pairs and will fine-tune on it, so article text is also training input. The writing style is learned from our own published posts, not from yours. Nothing from your feed is redistributed. We need pricing for both scopes.
>
> **Scale.** ~300–500 articles/day ingested, ~10 posts/day published. One organisation, internal use, with a possible later phase as a service for other experts — happy to discuss both licence scopes now rather than renegotiate later.
>
> **Questions.**
>
> *Coverage*
> 1. For each of these domains, do you return the full article body, a snippet, metadata only, or nothing: nytimes.com, ft.com, economist.com, telegraph.co.uk, thetimes.com, foreignaffairs.com, bloomberg.com, wsj.com, washingtonpost.com?
> 2. Which of those have full body text in the archive, and how far back?
>
> *Rights*
> 3. Does the licence permit using full article text as fine-tuning data, and separately as runtime context? Please price both.
> 4. May we store article bodies in our own database, and for how long?
> 5. What attribution is mandatory, and is there a limit on verbatim quotation in a published post?
>
> *Archive*
> 6. How deep is the archive **with full body text**, per publisher?
> 7. Does it cover March 2026 and earlier? We need to reconstruct a historical candidate pool.
> 8. Can we bulk-export all articles from a given set of domains for specific dates, and at what cost?
>
> *Commercial*
> 9. Price per 1,000 full-text articles, recent vs archival.
> 10. Is API access included in a base subscription or licensed separately?
> 11. Trial access: duration and volume.
>
> I've attached 24 article URLs from our own editorial history, spread across the publishers above. If you can run them through your API and share what comes back, that answers most of the above faster than a call.
>
> Best regards,
> [Ім'я] · [посада] · Office of the President, Kyiv School of Economics · [пошта]

*Додавати файл `posts_db/provider_acceptance.csv`.*

---

## 4. Рядки під конкретного вендора

Вставляти після абзацу «What we're building» — показує, що писали не шаблоном.

| Провайдер | Рядок |
|---|---|
| **Factiva** | *We understand WSJ is a Dow Jones title; we're particularly interested in whether Factiva Retrieval API covers the other publishers listed, and whether API access requires a separate agreement from a standard Factiva subscription.* |
| **Nexis Data+** | *Your Gen AI-approved dataset is exactly the licence category we need. Could you confirm which of the publishers listed above fall inside it, rather than the wider Nexis catalogue?* |
| **Opoint** | *Your 250,000 curated sources and sub-7-minute latency look like a strong fit for daily ingestion. Our main open question is full body text and licensing for AI-assisted drafting.* |
| **Webz.io** | *Your trust/credibility and political-slant tags could be useful signals in our relevance layer, beyond raw coverage.* |
| **Perigon** | *We've seen your MCP server and free tier — we'll start there and validate coverage ourselves. Reaching out about licensing terms for AI-assisted drafting and archive depth.* |

---

## 5. Нагадування за тиждень

> Hello [Ім'я],
>
> Following up on my note from [дата] about full-text coverage and licensing for AI-assisted editorial drafting.
>
> If a per-publisher coverage answer takes time, even trial credentials would help — we can run our 24-article test set ourselves and come back with specifics.
>
> Thanks,
> [Ім'я]

---

## 6. Дві речі, які варто узгодити до надсилання

**Ми тренуємо на статтях — це має бути в листі прямо.** База «стаття → пост» будується під fine-tune, тож текст видавця є вхідною стороною тренувальних пар. Ліцензія на тренування дорожча за grounded access, а частина видавців (зокрема NYT) її не продає в принципі. Просимо ціну на обидва сценарії окремо.

**Хто підписант.** Це комерційне листування від імені офісу. Варто, щоб Євгенія погодила формулювання про можливу платну фазу — воно впливає на те, яку ліцензію запропонують і за які гроші.
