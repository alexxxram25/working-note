# Why Jev, and why code around it

Working Note splits the work three ways. **Code counts. Jev judges. The reviewer writes.** This page is the evidence behind that design: what each claim rests on, which file in this repository implements it, and where it stops. Every claim either cites a source or points at a file here, and sections say plainly what is not shown.

The sources were read on 2026-10-11. Web pages change, so treat every figure from them as a dated snapshot, not a standing fact. Source numbers [1] to [5] match the numbered sources on the project's landing page; [6] is cited only here.

## The design in one paragraph

Code settles anything it can settle exactly: whether a line is material, whether the amounts add up, whether the text is a copy. Jev answers one yes or no question per criterion about the line's commentary and returns a probability, never text. The reviewer writes the standard and the feedback ahead of time, and the preparer reads the reviewer's words, verbatim. Each part does what it is good at, and none of them is asked to do what the sources say it is bad at.

## 1. Code does what code can check

**What the sources say.** OpenRouter's comparison of Jev with a chat-model judge says to use neither "if the code can check the criterion exactly", and its selection table lists schema, arithmetic and forbidden strings as deterministic checks: exact, free, and with no bias to test [3]. TypeSafe's own list of Jev's weak spots agrees. "Jev is not a calculator", it does not count reliably, and it reads dates as text, so TypeSafe says to keep arithmetic in code and to avoid asking the model something code can compute exactly [1].

**What Working Note does.** Before any line reaches Jev it passes a materiality gate and two checks in code: arithmetic (the amounts cited for the drivers sum to the variance within tolerance) and exact duplicates. The arithmetic check abstains when amounts cannot be parsed cleanly, such as ranges or percentages, rather than guessing. A third check, for near-duplicates, is a heuristic: it flags a line as likely, and the line is still graded. Only lines that are material and pass the arithmetic and duplicate checks are sent, and for each one only the row (account, comparison basis, prior, current and the change), the commentary, and each criterion's pass condition with a fixed instruction. See the README, "Who checks what" and "What leaves the sheet, and where it goes".

**What Jev is asked.** Whether something is present: a driver is named, evidence is pointed to, an amount is given for each driver. No starter criterion asks Jev to add, count or order dates. A criterion a team writes that needs any of those belongs in code as a rule, not in Jev as a question.

**Where this stops.** The near-duplicate check is a heuristic and is flagged as likely, not certain. Code cannot judge whether a driver is specific enough, which is why Jev is in the design at all.

## 2. Jev returns a probability and no text

**What the sources say.** Jev is a decision model. TypeSafe's weak-spot page says it is "not trained to generate text" [1]. OpenRouter describes it as non-generative, a model that "generates no text, no tokens", and says its responses are typed shapes, "never a completion, never a JSON body, never an explanation" [2]. OpenRouter's guide says the same [5], and so does TypeSafe's primer [6]. Each question is one of three types: Choice (an option, with a probability for each), Score (levels, with probabilities), or Noul, a yes or no question that returns the probability of yes [2][5].

**What Working Note does.** It uses one type only. The question builder turns each criterion into one Noul question ([question-builder.md](question-builder.md)). Because Jev returns no text, nothing a model wrote can reach a preparer. The feedback on a failed line is the text in the Criteria tab's "Feedback when failed" column, verbatim, and the criterion's feedback, name and rationale are never sent to Jev at all (README, "Who checks what"). That is a property of the interface, not an instruction in a prompt that a clever input could talk around.

**Where this stops.**

- A probability is still an opinion about the commentary. A wrong one can pass a weak line or flag a good one. Calibration (section 3) and reviewer overrides exist for that reason.
- Jev gives how sure it is and nothing else [3]. It cannot explain a failure, so the explanation has to be written by a person in advance, once per criterion. If a team wants an explanation specific to each line, a chat model is the right tool (section 6) and this design does not provide that.

## 3. Its probabilities can be checked

A chat model asked to grade writes its own confidence. The number is text the model chose to produce. Jev's probability is a claim about frequency: across many answers like this one, about that share should turn out acceptable. A claim like that can be tested against labels, and if it holds, a threshold means something [3].

OpenRouter published a test of exactly this. Its setup:

| | |
| --- | --- |
| Published by | OpenRouter, "Jev vs LLM-as-a-Judge" (9/21/2026, updated 9/24/2026) [3]. OpenRouter is one of the ways to reach Jev and the API this repository calls by default. |
| Run | 2026-09-21 through OpenRouter, by a coding agent, at a total cost of $0.041 |
| Faithfulness set | 50 rows from the HaluEval question-answering split, 100 answers. After review, 6 rows were dropped and 8 labels changed, leaving 88 items (52 acceptable, 36 not) |
| Summary set | 10 SummEval articles with 5 summaries each, rated by experts |
| Models | `typesafe/jev-1.13` (resolving to `typesafe/jev-1.13-20260917`) and a chat model at temperature 0 with JSON output |

What it found on the 88-item faithfulness set:

| Measure | Jev | Chat model |
| --- | --- | --- |
| Agreed with the labels | 84 | 83 |
| Precision | 0.98 | 0.98 |
| Brier score (lower is better) | 0.043 | 0.054 |
| Distinct confidence values | spread across the range | 6, all near 0 or near 1 |
| Cases scored 0.0 to 0.2 | 34, of which 3% were acceptable | 39, of which 10% were acceptable |
| Cases scored 0.8 to 1.0 | 42, of which 98% were acceptable | 49, of which 98% were acceptable |
| Cases between 0.3 and 0.7 | 7 | none |
| Cost per 1,000 judgments | $0.021 | $0.114 |
| Median latency | 171 ms | 1,662 ms |

The post's reading: accuracy was a tie, so accuracy is the wrong lens. The chat model "had no middle ground", while Jev put four cases between 0.4 and 0.6 and got half of them right, which is what a 0.5 should mean. Cases in the middle are the ones worth sending to a person, and only Jev produced any [3].

**What this supports.** For a closed question with the evidence in the request, Jev's probabilities behaved like frequencies in this sample, so a threshold set from labeled data is meaningful.

**What it does not support.**

- That Jev is more accurate. It was level (84 against 83), and two of the misses were shared by both models, which the post attributes to wrong labels.
- That the figures hold for commentary. The test used short passages and question answering, not variance commentary.
- Statistical weight. It is one run on 88 items. The post reports no confidence intervals.
- Independence. The post is published by an API provider that offers Jev, and a coding agent ran the experiment. Nothing here suggests the numbers are wrong, but it is not a neutral benchmark.
- The post's own caveat: calibration depends on the exact task, so measure it on your own labels before choosing a threshold [3].

## 4. The published method, and where Working Note follows it

The same post ends with a method for setting a threshold on your own data [3]. Working Note's calibration (README, "Criteria and calibration" and setup step 7) follows the shape of it, with differences worth knowing.

| Step in the post | In Working Note | Difference or limit |
| --- | --- | --- |
| Label 50 to 100 representative items against a written definition of acceptable | The reviewer labels past lines in onboarding; each criterion needs at least six fails and six passes to be calibrated | **Smaller.** Onboarding suggests 15 to 20 lines to start. Six per side is the least at which a threshold will be computed, not a recommended sample size. More labels are better. Fernhill's labeled test set (112 texts) is in the post's range. |
| Send each item to the judge and record the probability, latency and cost | `scripts/record_jev.py` records Jev's answers for the labeled lines | No second judge is run. This repository has not compared Jev with a chat model on commentary. |
| Build a reliability table and pick the lowest bin whose observed rate you would sign off on | `scripts/calibrate.py` calibrates each criterion on its own: a seeded 25% is held out, the threshold that maximizes balanced accuracy on the other 75% is chosen and checked on the held-out slice | **A different rule.** It selects by balanced accuracy, not by a sign-off rate. A criterion counts as Calibrated only if held-out balanced accuracy is at least 0.75 with at least two passes and two fails held out. |
| Send what falls between the accept and reject thresholds to a person | A flag on a calibrated criterion fails the line. A flag on an uncalibrated criterion becomes a prompt for the reviewer. Criteria Health lists criteria whose probabilities cluster near the threshold and suggests rewriting them | **No per-line middle band.** The reviewer is involved at two points: uncalibrated flags, and criteria that look vague. A line near a threshold is not routed to a person on its own. |
| Rerun the table whenever the rubric or the model version changes | A verdict depends on four things together: the model snapshot, the pass condition, the question builder's version and the threshold. Change any one and the criterion is Uncalibrated until it is measured again | Matches the advice. The model is pinned to a dated snapshot, and a calibration is tied to it. |

**Nothing ships calibrated.** Every Jev-graded criterion starts Uncalibrated, including the starter five. Their starting thresholds were measured on Fernhill Data, a fictional company, so they describe how Jev grades a made-up team's commentary, not yours. Until a team calibrates on its own labeled lines, preparers see the checks in code and Jev's flags go to the reviewer as prompts (README, "Who checks what" and "Criteria and calibration").

## 5. What TypeSafe lists as Jev's weak spots, and what Working Note does about each

TypeSafe publishes the known weak spots of `jev-1.13`, last reviewed 2026-10-02 [1]. Each one, in the page's order:

| Weak spot [1] | Status | What Working Note does |
| --- | --- | --- |
| Literal reading: it "answers the question you wrote, not the one you meant" | Partly | Question builder qb-1.1.0 asks for the reading a careful reviewer would give, and C1 was reworded after a blind check ([question-builder.md](question-builder.md), [evidence](evidence/c1-blind-check/)). Known remaining misses, in the README's words: C1's blind check missed the one line that names only the vendor that billed the account plus an amount, and "C5 still fails a single driver sized only by the total" (README, "Criteria and calibration"). |
| Math and numbers; counting | Designed around | Arithmetic runs in code before Jev is asked (section 1). Jev is asked whether an amount is present, not to compute with it. |
| Date and time comparison: it "reads dates as text, not as ordered quantities" | Designed around | The period is never sent, and no starter criterion asks Jev to compare or order dates. A team's own criterion that did should be a code rule. |
| Indirection and double negatives | Designed around, not measured | Each question is one criterion with a direct condition. Onboarding rejects any criterion that cannot be asked as a yes or no question. TypeSafe also says to avoid hiding several judgments in one question, which is why each criterion is its own question. |
| Large state full of irrelevant detail; bounded context | Designed around | Only the row's figures and the commentary are sent, plus one criterion's condition per question. OpenRouter gives the context length as 32,000 tokens [5]; a row, a commentary and one criterion are far below that. That is reasoning, not a measurement. |
| Adversarial content: state is data, and Jev "does not treat it as hostile by default" | **Not addressed** | See below. |
| Contradictory instructions and criteria | Partly | In each question the true criterion is the reviewer's "Passes when" text and the false criterion is a fixed sentence, so true maps to yes; TypeSafe warns that a question where true maps to no performs worse. A pass condition that conflicts with the fixed instruction is not checked automatically. |
| Choice option order | Not applicable | Working Note uses yes or no (Noul) questions only. |
| Generation | By design | Jev is never asked to generate. That is the point of section 2. |

**Adversarial content, in more detail.** The commentary a preparer writes is sent to Jev as data. TypeSafe says content written to steer the model, whether an injected instruction, a misleading framing or text that argues for its own classification, can move the answer, and advises being explicit in the criteria and testing before wide deployment [1]. Nothing in this repository's tests submits adversarial commentary, and the README does not discuss it. The repository has seen a related effect by accident: adding specific-sounding detail such as an amount and dates to a vague line lifted it over C1's threshold, and C1 was reworded to resist it (README, "Criteria and calibration"). OpenRouter's padding test found a small version of the same drift: padding wrong answers with true sentences raised Jev's average probability of acceptable from 0.08 to 0.12 [3].

What limits the damage: Jev cannot write or act, each question covers one criterion, a flag on an uncalibrated criterion is only a prompt, and the tool is a coaching tool, not a control: it never replaces the review (README). The realistic worst case is a line that passes when it should not. A team that cares should test its own edge cases before relying on flags.

## 6. Where a chat model is better, and why that does not change this design

The same post is clear about where a chat model wins [3]:

- On the 50 SummEval summaries, the chat judge tracked expert ratings of factual consistency much more closely (0.72 against 0.47, Spearman). Coherence was close to a tie (0.44 against 0.42). Jev still caught all three summaries the experts rated below 4 on consistency, with no false alarms on the other 47, so it works as a rough gate, but the chat model's ranking is the better signal.
- Open rubrics: if the criteria cannot be written down, a generative judge is needed, because Jev needs them spelled out.
- Written explanations: if a reviewer needs to know why something failed, the generative judge's reasoning is the product.
- Long-context, claim-by-claim checks, which suit step-by-step generation.

The post's advice is to pick one tool per criterion, not one per project: code where code can check exactly, Jev for closed criteria with the evidence in the request, a chat model for open or explanation-heavy ones, and a person when a decision is consequential and novel.

Working Note's criteria are written pass conditions, the evidence (the row and the commentary) is in the request, and the explanation is the reviewer's own feedback, written ahead of time. Consequential judgment stays with the reviewer. A chat model does have a place here: onboarding, where the work is open-ended and a person reads the result. An assistant interviews the reviewer, drafts criteria and rejects any that cannot be asked as a yes or no question, and writes the tabs, while the reviewer marks the labeled sample and confirms materiality (README, "Onboarding a team"). The trade is real. A chat model could write a reason for each failed line. This design gives up that convenience on purpose, so that no model's words reach a preparer.

## 7. Cost and speed

On the faithfulness set Jev cost $0.021 per 1,000 judgments against $0.114, and answered in a median 171 ms against 1,662 ms [3]. The post is candid that the cost gap is small at small volumes: for a nightly evaluation of 1,000 items it puts the difference at about $34 a year, and says the speed gap is what matters when a judge blocks a response. These are figures from short question-answering items. No Working Note cost or latency figure is claimed here.

## 8. What this page does not claim

- That Jev is more accurate than a chat model. In the one test cited, the two were level.
- That the cited figures hold for commentary on a flux line.
- That a team's thresholds are calibrated out of the box. They are not (section 4).
- That the design is robust to adversarial commentary. It has not been tested (section 5).
- That a source is independent. Of the six, three are from OpenRouter, an API provider that offers Jev, and three are from TypeSafe, the model's maker.

## Sources

Read 2026-10-11. The numbers match the sources on the project's landing page.

| # | Source | Used for |
| --- | --- | --- |
| 1 | TypeSafe, [Jev 1.13 known weaknesses](https://docs.typesafe.ai/model-jaggedness/jev-1.13) (last reviewed 2026-10-02) | Weak spots, "Keep the arithmetic in code", no text generation |
| 2 | OpenRouter, [What is Jev?](https://openrouter.ai/blog/insights/what-is-jev/) | Non-generative, typed answers, the three question types |
| 3 | OpenRouter, [Jev vs LLM-as-a-Judge](https://openrouter.ai/blog/tutorials/jev-vs-llm-as-a-judge/) | The test, its figures and limits, where a chat model wins, the threshold method, cost and speed |
| 4 | TypeSafe, [Confidence](https://docs.typesafe.ai/confidence) | Thresholds depend on the domain and the cost of an error; low confidence goes to a person |
| 5 | OpenRouter, [Jev guide](https://openrouter.ai/docs/guides/community/jev) | Typed answers with probabilities and no free-form text; context length |
| 6 | TypeSafe, [Machine learning primer](https://docs.typesafe.ai/introduction/machine-learning-primer) | The model does not generate text; it returns decisions and probabilities |

Source [4] says to start with conservative thresholds, test with your own data and adjust, and to route low confidence to a person. It does not describe calibrating on labeled lines or re-measuring when the model or wording changes, so those two points rest on source [3] and on this repository's own design.

## Keeping this page honest

- Re-read the sources when the pinned Jev snapshot changes. [1] applies to `jev-1.13`; a later version will have different weak spots.
- If the repository adds a test that submits adversarial commentary, update section 5 with the result, whichever way it goes.
- If a team runs a chat model against Jev on its own labeled lines, add the comparison to section 3. It is the missing piece.
