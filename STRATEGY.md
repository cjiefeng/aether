# STRATEGY.md: the owner's investment guidance

This file holds the owner's investment beliefs and portfolio rules. **Every portfolio review** must follow it, whether it's done by the owner, a Claude session or a milestone design.

**Scope.** These are the owner's views. Under the "No opinions in config or prompts" rule (CLAUDE.md, spec §1.2), this file is **never** copied into Aether's LLM prompts or config. Aether applies the rules only as **deterministic, numeric checks** (spec §6.10, M14): modality tags, concentration flags, red-flag monitors and gap detection. Prompts receive only the computed results, as facts and metrics.

Standing constraints that apply alongside the thesis (spec §1.4, §6.7.1, §6.7.2):
- **At most 9 names besides QTUM** (pure-plays + adjacent).
- **Market cap is one input, never the deciding factor.**
  - Small (< $2B): flag volatility, liquidity and dilution risk.
  - Mid ($2–50B): often the best balance.
  - Large (> $50B): quantum exposure likely diluted.
- **Hyperscalers and cloud platforms are excluded.**
- **US-listed only.**
- **Proposals only:** Aether never places trades, and the owner decides.

---

## Quantum Thesis

*Recorded 2026-10-08 from the owner's own words.*

### Core belief
My conviction is in quantum computing as a **technology**, not in any single company winning. The portfolio should be built to capture the technology's success regardless of which company or approach wins.

### Key risk: today's leader is often not tomorrow's winner
In early technologies, early leaders frequently lose (e.g. AltaVista and Netscape in the 1990s internet era). Quantum is still at that stage. It also has an extra layer of uncertainty: it is not yet known which hardware approach (modality) will win:
- **Superconducting:** e.g. IBM, Rigetti
- **Trapped ion:** e.g. IonQ, Quantinuum
- **Neutral atom:** e.g. QuEra, Atom Computing, Pasqal, Infleqtion
- **Photonic:** e.g. PsiQuantum, Xanadu
- **Annealing** (narrower/specialized): e.g. D-Wave

Buying "the current leader" is often an unintentional bet on one modality. "Market leader" is a starting point for research, never by itself a reason to buy.

### Portfolio rules
1. **Diversify across modalities, not just companies.** Hold names across several approaches. Accept that some will go to zero and that the eventual winner can cover those losses.
2. **Include picks-and-shovels suppliers** that get paid whichever modality wins: cryogenics, photonics/lasers, control and test electronics, post-quantum cryptography, quantum sensing. These are the purest expression of conviction in the technology itself, not a hedge against it.
3. **Keep each position modest and build over time** (stepwise buying, e.g. monthly). Treat sector-wide drawdowns as chances to add, not reasons to exit, as long as the thesis is intact.
4. **Watch for a winner to emerge, then increase weight.** The signals are:
   - working error-corrected machines
   - recurring revenue from paying customers
   - an end to heavy share issuance and dilution
5. **Exclude hyperscalers** (AWS, Microsoft, Google, etc.) deliberately. This also applies when evaluating ETFs: flag any fund whose holdings are heavy in hyperscalers.
6. **Consider market cap as one input** (alongside quantum revenue exposure, evidence strength and overlap), never the sole factor.

### Red flags to monitor for every holding
- Repeated share issuance diluting existing shareholders
- Cash runway under ~2 years at the current burn rate
- Revenue flat or missing expectations while spending rises sharply
- Large cash-draining acquisitions without clear revenue payoff

### How a review applies it
- **Categories:** tag every holding by modality, or by picks-and-shovels sub-industry, and show the % weight in each.
- **Concentration:** flag a single modality or a single company dominating the portfolio.
- **Red flags:** check every holding against the list above with current, dated, cited data.
- **Gaps:** name the modalities and supplier categories with no exposure.
- **Proposals only.** Within the 9-name cap, removals need serious, cited bad news (§6.7.2), and a strong #10 is a notification for the owner, not an add.
