# Task 3 qualitative cases

Selection rules (stated, fixed):
(1) selection disagreement: first 3 held-out questions per model (split order) where, at N=32 in order 0,
    self-consistency and the model's best quality+RWC method (SelfCert+RWC for MATH, VisConf+RWC for MathVista)
    choose different answers.
(2) revision: the first 2 live questions (split order) for each of the transitions W->C and C->W, plus the first
    C->C and W->W question.
(3) premature stopping: first 3 held-out (question, order) incorrect early stops per adaptive family per model,
    preferring ones where fixed-32 SC would have been correct.


## (llm) selection disagreement: SC vs SelfCert+RWC, N=32, order 0

- **test:2460** gold=`154` | SC→`58` (✗) | SelfCert+RWC→`154` (✓) | top groups {'154': 11, '58': 11, 'NO_ANSWER': 3, '45': 1, '98': 1} | pool correct 0.34
- **test:2892** gold=`-71` | SC→`1` (✗) | SelfCert+RWC→`0` (✗) | top groups {'0': 4, '1': 4, '23': 2, '-1': 1, 'NO_ANSWER': 1} | pool correct 0.00
- **test:2948** gold=`1+\sqrt 2,\ 1-\sqrt 2,\ 2+\sqrt 3,\ 2-\sqrt 3` | SC→`1` (✗) | SelfCert+RWC→`\frac{3 + \sqrt{13 + 4\sqrt{3}}}{2}, \frac{3 - \sqrt{13 + 4\sqrt{3}}}{2}, \frac{3 + \sqrt{13 - 4\sqrt{3}}}{2}, \frac{3 - \sqrt{13 - 4\sqrt{3}}}{2}` (✗) | top groups {'1': 5, '1 \\text{ and } 2': 4, '2 + \\sqrt{3}, 2 - \\sqrt{3}': 3, '\\frac{3 + \\sqrt{13 + 4\\sqrt{3}}}{2}, \\frac{3 - \\sqrt{13 + 4\\sqrt{3}}}{2}, \\frac{3 + \\sqrt{13 - 4\\sqrt{3}}}{2}, \\frac{3 - \\sqrt{13 - 4\\sqrt{3}}}{2}': 3, 'NO_ANSWER': 2} | pool correct 0.00

## (llm) premature stops - agreement

- test:2460 order 7: stopped at n=4 with `58` (lead 0.75, margin 0.33); fixed-32 → `154` (correct=True); gold `154`; pool correct 0.34
- test:534 order 6: stopped at n=4 with `48` (lead 0.75, margin 0.00); fixed-32 → `56` (correct=True); gold `56`; pool correct 0.72
- test:3409 order 4: stopped at n=8 with `0` (lead 0.62, margin 0.11); fixed-32 → `3` (correct=True); gold `3`; pool correct 0.66

## (llm) premature stops - quality_aware

- test:3504 order 7: stopped at n=4 with `120` (lead 0.50, margin 0.67); fixed-32 → `216` (correct=True); gold `216`; pool correct 0.41
- test:2460 order 7: stopped at n=4 with `58` (lead 0.75, margin 0.33); fixed-32 → `154` (correct=True); gold `154`; pool correct 0.34
- test:534 order 2: stopped at n=4 with `48` (lead 0.50, margin 0.00); fixed-32 → `56` (correct=True); gold `56`; pool correct 0.72

## (vlm) selection disagreement: SC vs VisConf+RWC, N=32, order 0

- **552** gold=`4` | SC→`4` (✓) | VisConf+RWC→`2` (✗) | top groups {'4': 14, '3': 9, '2': 9} | pool correct 0.44
- **643** gold=`A` | SC→`C` (✗) | VisConf+RWC→`A` (✓) | top groups {'C': 11, 'A': 10, 'NO_ANSWER': 8, 'D': 3} | pool correct 0.31
- **542** gold=`2` | SC→`2` (✓) | VisConf+RWC→`3` (✗) | top groups {'2': 12, '3': 11, '5': 3, '1': 2, '6': 1} | pool correct 0.38

## (vlm) premature stops - agreement

- 358 order 4: stopped at n=4 with `D` (lead 0.50, margin 0.33); fixed-32 → `A` (correct=True); gold `A`; pool correct 0.44
- 358 order 5: stopped at n=4 with `D` (lead 0.50, margin 0.17); fixed-32 → `A` (correct=True); gold `A`; pool correct 0.44
- 358 order 6: stopped at n=4 with `E` (lead 0.50, margin 0.67); fixed-32 → `A` (correct=True); gold `A`; pool correct 0.44

## (vlm) premature stops - quality_aware

- 358 order 4: stopped at n=4 with `D` (lead 0.50, margin 0.33); fixed-32 → `A` (correct=True); gold `A`; pool correct 0.44
- 358 order 5: stopped at n=4 with `D` (lead 0.50, margin 0.17); fixed-32 → `A` (correct=True); gold `A`; pool correct 0.44
- 358 order 6: stopped at n=4 with `E` (lead 0.50, margin 0.67); fixed-32 → `A` (correct=True); gold `A`; pool correct 0.44

## (live) refinement transitions

- **W->C** test:196: initial boxed `None` → revised `81`; revision excerpt: "To determine the number of lattice points within or on the circle of radius 5 centered at the origin \((0,0)\), we need to count all integer coordinate points \((x, y)\) that satisfy the inequality \(x^2 + y^2 \leq 25\).  We will systematically check each possible integer value for \(x\) and find th…"
- **C->W** test:534: initial boxed `56` → revised `70`; revision excerpt: "Let's carefully review the solution step-by-step to ensure there are no errors.  Given: 1. Frank answered 80 questions. 2. Each correct answer scores 5 points. 3. Each incorrect answer scores -2 points. 4. Each unanswered question scores 0 points. 5. Frank's total score is 232 points.  We define: - …"
- **C->C** test:943: initial boxed `14` → revised `14`; revision excerpt: "Let's carefully review the solution step-by-step to ensure accuracy.  We start with the equation for the arithmetic mean: \[ \frac{A + B + C}{3} = 10 \] Multiplying both sides by 3, we get: \[ A + B + C = 30 \]  Next, we use the given relationships: \[ A = B - 6 \] \[ C = B + 3 \]  Substitute these …"
- **W->W** test:2688: initial boxed `-\frac{25}{16}` → revised `-\frac{25}{16}`; revision excerpt: "Let's carefully re-examine the steps to ensure the solution is correct and identify any potential errors.  We start with the function \( f(x) = x(x + 1)(x + 2)(x + 3) \). To simplify the expression, we can pair the terms:  \[ f(x) = x(x + 3)(x + 1)(x + 2) \]  Rewriting it in terms of \( y = x + \fra…"