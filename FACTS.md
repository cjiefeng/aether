# FACTS

<!-- Generated from config/facts.yaml by `make facts`. Edit the YAML, not this file. -->

Every factual claim seeded into Aether, with its source and verification status.
Statuses: `unverified` → `verified_by_claude` → `signed_off` (owner, end of M3).
Only `signed_off` facts reach LLM prompts as facts; the rest are labelled UNCONFIRMED.

| ID | Claim | Sources | Retrieved | Status |
|---|---|---|---|---|
| `ionq_revenue_fy2025_guidance_2026` | IonQ FY2025 revenue ≈ $130.0M; 2026 guidance later raised to ≈ $285M (Q2-26 report) | https://gp.advalorem.io/insights/2026-04-28.html<br>https://247wallst.com/investing/2026/09/30/one-year-later-only-one-quantum-stock-call-paid-off-heres-what-changed/ | 2026-10 | UNVERIFIED |
| `ionq_acquire_skywater` | IonQ agreed to acquire SkyWater Technology | https://gp.advalorem.io/insights/2026-04-28.html | 2026-10 | UNVERIFIED |
| `qnt_ipo` | Quantinuum IPO on Nasdaq as QNT, June 2026, ~$14.3B market cap at top of range; Honeywell ≈ 49.1% voting power post-IPO | https://thequantuminsider.com/2026/06/02/quantinuum-expands-ipo-as-valuation-climbs-above-14-billion/ | 2026-10 | UNVERIFIED |
| `qnt_lockup_expiry` | QNT lock-up expiry date: UNKNOWN | — | — | UNVERIFIED |
| `infq_listing` | Infleqtion trades as INFQ; listing route/date and any warrants/earn-outs | https://fool.com/investing/2026/07/22/ionq-vs-quantinuum-vs-infleqtion-vs-rigetti-vs-d-w/ | 2026-10 | UNVERIFIED |
| `darpa_qbi_stage_b` | DARPA QBI Stage B (Nov 2025): Atom Computing, Diraq, IBM, IonQ, Nord Quantique, Photonic, Quantinuum, Quantum Motion, QuEra, Silicon Quantum Computing, Xanadu | https://hpcwire.com/2025/11/07/darpa-selects-11-participants-for-quantum-benchmarking-initiative-stage-b/<br>https://darpa.mil/research/programs/quantum-benchmarking-initiative/stage-b-selection | 2026-10 | UNVERIFIED |
| `ibm_roadmap_ftqc` | IBM roadmap: Kookaburra 2026, Cockatoo 2027, Starling 2029 (200 logical qubits, 1e8 gates) | https://ibm.com/quantum/blog/large-scale-ftqc | 2026-10 | UNVERIFIED |
| `pqc_deadlines` | NIST IR 8547 (draft): deprecate quantum-vulnerable algorithms after 2030, disallow after 2035; EU roadmap: high-risk systems by end-2030, rest by 2035 | https://insidedeeptech.com/how-many-qubits-to-break-rsa-2048/ | 2026-10 | UNVERIFIED |

## Notes

- `ionq_revenue_fy2025_guidance_2026`: Secondary sources only; verify against IonQ 10-K FY2025 and the Q2-26 8-K Item 2.02 on EDGAR.
- `ionq_acquire_skywater`: Verify via IonQ / SkyWater 8-K on EDGAR.
- `qnt_ipo`: Verify ticker, date and Honeywell voting power against the final 424B4 on EDGAR.
- `qnt_lockup_expiry`: Open question. Derive from the final 424B4 prospectus on EDGAR (M2). Do not guess.
- `infq_listing`: Verify via EDGAR (listing route, date, warrants, earn-outs).
- `darpa_qbi_stage_b`: Verify membership list against the DARPA page.
- `ibm_roadmap_ftqc`: Verify against the IBM blog post.
- `pqc_deadlines`: Secondary source; verify against NIST IR 8547 and the EU PQC roadmap directly.

## Open questions

- `qnt_lockup_expiry`: Open question. Derive from the final 424B4 prospectus on EDGAR (M2). Do not guess.
