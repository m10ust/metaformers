# Metaformers

Metaformers is an experimental AI orchestration stack designed to **set a conversation in motion and let it evolve**.  
Starting from a single **initial prompt** — the *seed question* — three models engage in a structured, iterative dialogue loop, building on each other’s outputs until entirely new insights and behaviors emerge.

---

## 🌊 The Metaformers Loop

The **loop** is the heart of Metaformers.  
It follows a simple but powerful arc:

1. **Initial Prompt**  
   A single, well-crafted question or statement — the seed that starts the flow.  
   Example:  
   > "Using local LLMs running on PyTorch to get emergent behaviors from a PostgreSQL database and Python."

2. **Agent Turn-Taking**  
   - **LLaMA 2 / Dolphin3** reason through the problem, propose steps, ask clarifying questions.  
   - **GPT-OSS** reframes or contrasts ideas to inject new perspectives.  
   - Each model responds to the *previous* model’s output, creating a chain reaction of refinements.

3. **Emergent Behavior Phase**  
   Around turn 15–30, patterns and unexpected strategies start appearing — the models surprise even their human observer.

4. **Actionable Blueprinting**  
   The loop naturally shifts toward concrete implementation steps, database queries, code snippets, and structured execution plans.

---

## 🚀 Features
- **Three-Model Synergy** — Local LLaMA2, Dolphin3, and GPT-OSS in a continuous dialogue loop.
- **PostgreSQL Context** — Real-time queries and reasoning over structured datasets.
- **Emergent Reasoning** — Watch unplanned insights surface from the interaction.
- **Agent Autonomy Simulation** — Models appear to “debate” and “collaborate” without manual intervention mid-loop.
- **Full Local Control** — Runs entirely on local PyTorch, GPU-accelerated.

---

## 📦 Stack Components
| Component         | Purpose |
|-------------------|---------|
| **PyTorch**       | Core tensor engine for model execution |
| **LLaMA 2 / Dolphin3** | Main reasoning agents |
| **GPT-OSS**       | Open-source GPT-compatible agent for perspective contrast and synthesis |
| **PostgreSQL**    | Data source for context injection |
| **Python**        | Orchestration of turns, logging, and DB access |
| **JSON Logs**     | Structured iteration transcripts |

---

## 🧠 Example Loop Flow

[Turn 01] LLaMA2: The seed prompt suggests a data-driven emergent strategy…
[Turn 02] Dolphin3: Let’s formalize this into a 3-step process involving…
[Turn 03] GPT-OSS: Adding a recursive data shaping layer could amplify…
…
[Turn 24] LLaMA2: This resembles a meta-agent evolution cycle. We should…
[Turn 25] Dolphin3: That implies adding a memory bank to the PostgreSQL schema…
[Turn 26] GPT-OSS: Yes — and here’s the pseudo-code to implement it.

Use it in a responsible manner. Change models as needed in the scripts to fit your stack.
