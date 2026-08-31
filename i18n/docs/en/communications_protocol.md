# Communications Protocol for Idea Evaluation

All subsequent agents working within the Windows Context Menu customization ecosystem must adhere to this structured protocol when challenging, evaluating, or refining proposed ideas.

## 1. The "Steel-Man" Doctrine
Before challenging an idea, the evaluating agent must construct the strongest possible version of the original proposal.
- Articulate the core value proposition clearer than the original author.
- Identify at least one unstated benefit of the approach.

## 2. Red-Teaming (Vulnerability Assessment)
Once the idea is steel-manned, agents must challenge it across the following vectors:
- **Ecosystem Fit:** Does this feel like a native context-menu tool, or is it trying to be a full application?
- **Performance:** What happens if this is accidentally run on a directory with 100,000 files?
- **Destructiveness:** Is there a risk of unrecoverable data loss?
- **Dependency Overhead:** Does this require excessive external dependencies (e.g., massive Python libraries or uninstalled binaries)?

## 3. The Rebuttal Format
Any critique must be structured as follows:
- **Hypothesis:** What the idea aims to solve.
- **Vulnerability:** The specific flaw or risk identified.
- **Alternative Formulation:** A proposed pivot that retains the value while mitigating the risk.

## 4. Final Verdict Mechanism
Ideas must not be outright rejected without proposing a pivot unless they pose a catastrophic risk to the file system (e.g., untracked recursive deletion).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
