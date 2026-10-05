# FACTS

<!-- Generated from config/facts.yaml by `make facts`. Edit the YAML, not this file. -->

Every factual claim seeded into Aether, with its source and verification status.
Statuses: `unverified` → `verified_by_claude` → `signed_off` (owner, end of M3).
Only `signed_off` facts reach LLM prompts as facts; the rest are labelled UNCONFIRMED.

| ID | Claim | Sources | Retrieved | Status |
|---|---|---|---|---|
| `ionq_revenue_fy2025_guidance_2026` | IonQ FY2025 revenue $130.0M (8-K Ex. 99.1, 2026-02-25); FY2026 revenue guidance raised to $280-290M, midpoint $285M, in the Q2-26 results (8-K Ex. 99.1, 2026-08-05) | https://www.sec.gov/Archives/edgar/data/1824920/000119312526071520/ionq-ex99_1.htm<br>https://www.sec.gov/Archives/edgar/data/1824920/000119312526335040/ionq-ex99_1.htm<br>https://gp.advalorem.io/insights/2026-04-28.html<br>https://247wallst.com/investing/2026/09/30/one-year-later-only-one-quantum-stock-call-paid-off-heres-what-changed/ | 2026-10-04 | SIGNED OFF |
| `ionq_acquire_skywater` | IonQ agreed to acquire SkyWater Technology (merger agreement dated 2026-01-25, 8-K Item 1.01); IonQ's Q2-26 release (2026-08-05) says the acquisition has closed | https://www.sec.gov/Archives/edgar/data/1824920/000119312526021616/d10479d8k.htm<br>https://www.sec.gov/Archives/edgar/data/1824920/000119312526335040/ionq-ex99_1.htm<br>https://gp.advalorem.io/insights/2026-04-28.html | 2026-10-04 | VERIFIED BY CLAUDE (awaiting owner sign-off) |
| `qnt_ipo` | Quantinuum Inc. Class A common stock listed on Nasdaq as QNT; final prospectus dated 2026-06-03; 28,000,000 Class A shares at $60.00; Honeywell Entities to hold about 47.8% of combined voting power after the offering (47.0% if the underwriters' option is exercised in full) | https://www.sec.gov/Archives/edgar/data/2110105/000162828026041003/quantinuum-424b4.htm<br>https://thequantuminsider.com/2026/06/02/quantinuum-expands-ipo-as-valuation-climbs-above-14-billion/ | 2026-10-04 | SIGNED OFF |
| `qnt_lockup_expiry` | QNT IPO lock-up: 180 days after the 2026-06-03 prospectus, i.e. ends 2026-11-30; J.P. Morgan and Morgan Stanley may release locked-up shares early at their discretion | https://www.sec.gov/Archives/edgar/data/2110105/000162828026041003/quantinuum-424b4.htm | 2026-10-04 | VERIFIED BY CLAUDE (awaiting owner sign-off) |
| `infq_listing` | Infleqtion, Inc. listed on the NYSE as INFQ (warrants INFQ WS, exercisable at $11.50) through a merger with Churchill Capital Corp X (a SPAC) consummated 2026-02-13; closing 8-K filed 2026-02-17. Registration-rights holders agreed to a 180-day transfer restriction from closing, ending early if the VWAP is at least $12.00 for 15 trading days | https://www.sec.gov/Archives/edgar/data/2007825/000119312526053097/d900344d8k.htm<br>https://fool.com/investing/2026/07/22/ionq-vs-quantinuum-vs-infleqtion-vs-rigetti-vs-d-w/ | 2026-10-04 | VERIFIED BY CLAUDE (awaiting owner sign-off) |
| `darpa_qbi_stage_b` | DARPA QBI Stage B (as of 2025-11-06): Atom Computing, Diraq, IBM, IonQ, Nord Quantique, Photonic, Quantinuum, Quantum Motion, QuEra, Silicon Quantum Computing, Xanadu | https://darpa.mil/research/programs/quantum-benchmarking-initiative/stage-b-selection<br>https://hpcwire.com/2025/11/07/darpa-selects-11-participants-for-quantum-benchmarking-initiative-stage-b/ | 2026-10-04 | SIGNED OFF |
| `ibm_roadmap_ftqc` | IBM roadmap (blog, 2025-06-10): Kookaburra 2026, Cockatoo 2027, Starling 2029 (200 logical qubits, 100 million gates) | https://ibm.com/quantum/blog/large-scale-ftqc | 2026-10-04 | SIGNED OFF |
| `pqc_deadlines` | NIST IR 8547 (initial public draft, Nov 2024): quantum-vulnerable RSA/ECDSA/ECDH/DH at 112-bit strength deprecated after 2030, all quantum-vulnerable public-key algorithms disallowed after 2035. EU coordinated PQC roadmap (v1.1, June 2025): high-risk use cases transitioned no later than end-2030; as many systems as feasible (medium-risk) by end-2035 | https://nvlpubs.nist.gov/nistpubs/ir/2024/NIST.IR.8547.ipd.pdf<br>https://digital-strategy.ec.europa.eu/en/library/coordinated-implementation-roadmap-transition-post-quantum-cryptography<br>https://insidedeeptech.com/how-many-qubits-to-break-rsa-2048/ | 2026-10-04 | SIGNED OFF |

## Notes

- `ionq_revenue_fy2025_guidance_2026`: Matches the seed. Both figures are company press releases furnished on 8-K (Item 2.02), not audited filings; the FY2025 10-K (2026-02-25) is the audited source. The Q2-26 release says the outlook excludes any SkyWater contribution.
- `ionq_acquire_skywater`: Agreement verified on the 8-K. Closing is stated in IonQ's own release; the closing date itself was not checked against a closing 8-K.
- `qnt_ipo`: Corrected from the seed. DISCREPANCY: the seed's Honeywell voting power (about 49.1%) differs from the 424B4 (47.8% / 47.0%). The seed's ~$14.3B market cap 'at top of range' predates pricing and is not stated in the 424B4, so it was dropped from the claim.
- `qnt_lockup_expiry`: Extracted deterministically from the 424B4 text by `edgar.text.extract_lockup` (test fixture: recorded 424B4). Day count: 2026-06-03 + 180 days = 2026-11-30. Lock-ups usually end at the open of the next trading day; check the exact first-sale date before relying on it.
- `infq_listing`: Listing route, date, exchange and warrants verified on the closing 8-K. The closing 8-K does not mention earn-outs; whether any earn-out exists is an open question.
- `darpa_qbi_stage_b`: All 11 names match the DARPA page, which dates the list 'as of Nov. 6, 2025'.
- `ibm_roadmap_ftqc`: Matches the blog post. It also places a Starling magic-state-injection demonstration in 2028. These are roadmap targets, not delivered milestones.
- `pqc_deadlines`: Refined from the seed: the NIST 2030 'deprecated' date applies only to 112-bit security strength (>=128-bit goes straight to 'disallowed after 2035'), and NIST IR 8547 is still a draft on the CSRC page. EU wording checked in the roadmap PDF linked from the Commission page.

## Open questions

- `ionq_acquire_skywater`: SkyWater closing date and final consideration: confirm on IonQ's closing 8-K (Item 2.01).
- `qnt_lockup_expiry`: Exact first trading day locked-up QNT shares can be sold (2026-11-30 or 2026-12-01), and any early release announced by the underwriters.
- `infq_listing`: Infleqtion earn-out shares (if any): read the S-4 / proxy statement/prospectus for the Churchill Capital Corp X merger.
