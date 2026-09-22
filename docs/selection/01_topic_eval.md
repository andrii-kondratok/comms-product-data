# Тематичний відбір: перевірка на даних

Модель `BAAI/bge-m3`, 18 новинних тем, 66 фасетів. Позитиви — дайджест (2,727), негативи — пул кандидатів, що не потрапив у дайджест (3,008).

## Розділення

**AUC = 0.816** (0.5 — випадково, 1.0 — ідеально).

| Поріг | Позитивів проходить | Негативів проходить | Скорочення потоку |
|---|---|---|---|
| 0.423 | 95% | 53% | ×1.9 |
| 0.443 | 90% | 44% | ×2.3 |
| 0.472 | 80% | 30% | ×3.4 |
| 0.494 | 70% | 22% | ×4.5 |

Робочий поріг — за 90% позитивів: **0.443**.

## Які теми живлять дайджест

| Тема | Дайджест | Пул | Частка взятих серед кандидатів цієї теми |
|---|---|---|---|
| 3. Репорти KSE Institute / дослідження KSE | 24 (1%) | 38 | 39% |
| 4. Санкції, як KSE залучена | 211 (8%) | 312 | 40% |
| 6. Цитування KSE в топ-виданнях, публікації людей KSE | 9 (0%) | 16 | 36% |
| 7. Лідерство в інженерії, математиці, економіці | 3 (0%) | 61 | 5% |
| 8. Армія (Хартія, допомога військовим, викладачі-військові) | 72 (3%) | 72 | 50% |
| 9. Внутрішня політика в Україні | 96 (4%) | 174 | 36% |
| 10. Зовнішня політика (США, Росія, Китай, Європа, Україна в глоб. контексті) | 894 (33%) | 566 | 61% |
| 11. Війна в Україні (досягнення, атаки РФ, оборонка) | 741 (27%) | 619 | 54% |
| 12. Стан економіки РФ | 177 (6%) | 180 | 50% |
| 14. Інтерв'ю й виступи Тимофія | 14 (1%) | 22 | 39% |
| 16. Скандали | 41 (2%) | 158 | 21% |
| 17. Хвалимо чи позоримо інші університети | 10 (0%) | 48 | 17% |
| 18. Корупція, антиплагіат, академічна доброчесність | 17 (1%) | 43 | 28% |
| 19. Історії людей | 241 (9%) | 238 | 50% |
| 20. Подяка і «переможка» | 34 (1%) | 158 | 18% |
| 21. ШІ в освіті/навчанні | 107 (4%) | 207 | 34% |
| 22. Цікаві теми (критичне мислення, ідеї, книги) | 34 (1%) | 96 | 26% |
| 23. Наука | 2 (0%) | 0 | 100% |

## Позитиви нижче порогу (273): що тематика не ловить

- `0.302` Перші кадрові рішення Драпатого - кого звільнили і кого призначили
- `0.364` Джулі Девіс завершує роботу на посаді тимчасово повіреної США в Україні
- `0.392` We must not forget the gathering evil in the east [inferred]
- `0.397` The Strong Do What They Can—and Suffer What They Must — What Thucydides Really Thought About Power
- `0.397` The far-right leader pushing Germans to forget the Nazi past — Hans-Thomas Tillschneider could soon wield more power than any other far-righ
- `0.397` Колишній власник Сенс Банку Михайло Фрідман хоче відсудити в України $1 мільярд. Ті, хто маніпулював «потоками» в банку або намагався це роб
- `0.402` Мадяр про захоплення грошей "Ощадбанку": Ми бачили пропагандистські новини
- `0.403` Хто з "Альтернативи для Німеччини" поїхав на форум Путіна
- `0.407` Judge refuses to block Pentagon from firing Stars and Stripes staffers over CBS interview — U.S. District Judge Trevor McFadden, a Trump app
- `0.411` Giorgia Meloni’s Populist Formula Failed — The Italian prime minister hasn’t convincingly delivered the renewal she once promised.
- `0.412` The delights of deadlines — A friend to procrastinators, an enemy to prattlers, a necessity for managers
- `0.415` USS Abraham Lincoln docks in Thailand after 9 months at sea — The crew will get a chance to “rest and recharge” in the beach destination of 
- `0.417` Zelensky focuses on American 'Patriots' in US Independence Day message
- `0.418` Germany’s spy agency comes in from the cold
- `0.419` Pentagon Commits Over $120 Billion for Patriot Missiles, Submarines
- `0.423` Silicon Valley’s Bad Bet on the Gulf — Why the AI Build-Out Was Doomed From the Start
- `0.425` This is a giant leap towards the real space age — Mankind has not landed on the moon since 1972, but if Artemis mission marks a serious effo
- `0.427` Ordinary Iranians Won’t See a Dime of Trump’s Money — As the public suffers, a corrupt regime prepares for a bonanza.
- `0.429` Houthis advance along Yemeni coast, threaten Saudi oil exports in the Red Sea
- `0.430` «Дождь»: в Бурятии власти требуют от предприятий отправлять сотрудников на войну — или заплатить по 100 тысяч за человека
- `0.431` Switzerland’s Three Strategic Dilemmas
- `0.433` «Дельфіни і Вельзевул» — Войцех Тохман про порятунок тварин у прифронтових зонах — Анастасія Лисиця / «Бабель»
- `0.433` «150 заявок на програму з Fire Point і жодна не пройшла». Президентка КАІ про вступну кампанію, співпрацю з бізнесом та вплив ШІ — Під час в
- `0.433` Сяйво не зійшло за графіком. Як Україна створює національний ШІ
- `0.436` Small aircraft crashes into Beijing’s tallest building, videos show — Photos showed a hole in the glass exterior of the 109-story CITIC Towe

## Негативи з найвищим балом

- `0.776` Ukraine Blasts Key Russian Oil Refinery, Hitting Processing Unit and Fuel Tanks
- `0.773` A crisis over using frozen Russian assets to help Ukraine
- `0.750` Ukrainian Drone Evades Russian Helicopter Fire and Strikes Sanctioned Oil Tanker Near Sochi
- `0.740` Ukraine Strikes Russian Drone Command Posts and Supply Depots
- `0.736` Ukraine’s valiant defence of Pokrovsk is nearing its end
- `0.736` How a Ukrainian strike on a Russian oil hub caused catastrophe
- `0.732` Russian Drone Strike Hits Kyiv-Warsaw Train Just 2 Kilometers From Polish Border
- `0.731` Ukraine is scaling up interceptor drones
- `0.726` Ukraine Strikes Major Russian TANECO Oil Refinery 1,200 Kilometers From Border
- `0.725` A Russian drone has revived a Ukrainian nuclear nightmare
- `0.720` Ukraine Carried Out Its Deepest Drone Strike On Russia Ever
- `0.720` A huge corruption scandal threatens Ukraine’s government
- `0.713` Russia Launches Large-Scale Missile and Drone Attack Across Ukraine, Killing at Least Four
- `0.710` Ukraine war latest: Kyiv-Warsaw train struck by Russian drone near Polish border as mass attack targets western Ukraine
- `0.708` Russian Central Bank Holds Key Rate at 14%

## Цілі: збіг із розміткою Post Metrics

Пар стаття → пост, де пост має Strategic Goal: 201. Найближча тема статті працює на ту саму ціль, що й пост: **89%**.

- ukraine_advocacy: war_in_ukraine 55, human_stories 41, foreign_policy 35, sanctions 18
- ethics: human_stories 24, sanctions 4, ai_education 3, ua_domestic_politics 3
- kse_advocacy: ai_education 3, human_stories 2, sanctions 2, war_in_ukraine 2
