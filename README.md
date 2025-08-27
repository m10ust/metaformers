# Metaformers

# Clone the repo
git clone https://github.com/m10ust/metaformers.git

cd metaformers

# Run a seed loop (Mac/BSD)
python metaformers_v5.py or ./metaformers_v5.py after doing chmod +x metaformers_v5.py

# Run on Linux
python metaformers_linux.py

----------------------------------------------------------------------------------------------

Metaformers is an experimental AI orchestration stack designed to **set a conversation in motion and let it evolve**.  
Starting from a single **initial prompt** — the *seed question* — three models engage in a structured, iterative dialogue loop, building on each other’s outputs until entirely new insights and behaviors emerge. Much like a live peer-reviewed whitepaper if you seed prompt is about science.

---

```mermaid

flowchart TD
  %% Core loop
  U[Seed / Prior Turn] --> Q[Questioner]
  Q -->|refines prompt| C[Creator]
  C -->|answer| S[Scriber]
  C -->|writes to log| L[(Log: master.md)]
  Q -->|writes to log| L
  M -->|writes to log| L
  S -->|summary| L
  C -->|optional NextPrompt| Q
  Q -->|every N turns| M[MediatorQ]
  M -->|meta-critique| Q
  L -.-> IDX[index.md]

  %% Styling
  classDef actor fill:#2563eb,stroke:#1e3a8a,color:#ffffff;
  classDef helper fill:#059669,stroke:#064e3b,color:#ffffff;
  classDef log fill:#f59e0b,stroke:#78350f,color:#000000;

  class U,Q,C,S,M actor;
  class L,IDX log;

```

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

## 📦Suggested Stack Components
| Component         | Purpose |
|-------------------|---------|
| **PyTorch**       | Core tensor engine for model execution |
| **LLaMA 2 / Dolphin3** | Open-source LLMs used as Questioner (Llama 2) and Mediator/Reviewer/wildcard (Dolphin3) for perspective contrast and wildcard injection  |
| **GPT-OSS**       | Open-source GPT-compatible agent donig the main reasoning and heavylifting |
| **PostgreSQL**    | Data source for context injection |
| **Python**        | Orchestration of turns, logging, and DB access |
| **JSON Logs**     | Structured iteration transcripts |

PS: The version 5 of the script (metaformers_v5.py) let you choose the model you want by listing all your model and letting you make a selection. Thought I would mention it. 

PS: You don't need PyTorch and PostgreSQL to test the loop. The two main scripts (metaformers_v5.py (MacOS and BSD-friendly) and metaformers_linux.py (Linux-friendly and can accomodate slower rigs) in the root folder are designed to work with only Python and Ollama + models of your choice installed localy. The PyTorch implementation is at early stage and for advanced users confortable with pgvector (PostgreSQL) and PyTorch. The ingest.py script is what you use to make ingestion of .txt files or whathave you, currently it is configured for txt files. Then you run the rag_chat.py to chat with the models on IE what they have ingested last etc... It's possible, actually 100% sure the scripts have my local configs so replace them if you want to use them. Building a strong database is essential here so everything works well. Unexperienced users can still use the Metaformers main scripts and have fun since it only uses Python and Ollama. Anyone with Python and Ollama installed can instantly start experimenting. This is the way Metaformers was designed to be experimented with in the first place. No Pytorch and PostgreSQL non-sense. Local & private + fully recursive :)

```mermaid
flowchart TD
    %% Metaformers Peer-Review Loop
    A[Seed / Prior Turn] --> Q["Questioner<br/>• Refines/presses claim<br/>• Poses 1 focused question"]
    Q --> C["Creator<br/>• Proposes answer & structure<br/>• May emit NextPrompt"]
    C -->|every N turns| M["MediatorQ<br/>• Meta-critique / sharpen next step"]
    C --> S["Scriber<br/>• 2–4 bullet summary<br/>• Captures claims & TODOs"]
    S --> L["Logger (master.md)<br/>• Turn header<br/>• Role blocks<br/>• NextTopic hint"]
    M --> L

    %% Feedback & Emergence
    L -->|AUTO_CHAIN on| Q
    C -. emergent prompts .-> Q
    Q -. anomaly flags .-> L

    %% Roles as “peer reviewers”
    subgraph ReviewDynamics[Peer-Review Dynamics]
      RV1[Questioner ≈ Reviewer #1<br/>skeptical probe]
      RV2[MediatorQ ≈ Reviewer #2<br/>scope/assumption check]
      CH[Archivist/Logger ≈ Program Chair<br/>tracks issues]
    end

    Q --- RV1
    M --- RV2
    L --- CH

    %% Health checks
    classDef role fill:#1f77b4,stroke:#0e3d66,color:#fff;
    classDef proc fill:#2ca02c,stroke:#135a13,color:#fff;
    classDef meta fill:#bcbd22,stroke:#6d6f14,color:#111;
    class Q,RV1 role
    class C proc
    class M,RV2 meta
    class S,L,CH meta
```
---

## 🧠 Example Loop Flow

[Turn 01] LLaMA2: The seed prompt suggests a data-driven emergent strategy…
[Turn 02] Dolphin3: Let’s formalize this into a 3-step process involving…
[Turn 03] GPT-OSS: Adding a recursive data shaping layer could amplify…
…
[Turn 24] LLaMA2: This resembles a meta-agent evolution cycle. We should…
[Turn 25] Dolphin3: That implies adding a memory bank to the PostgreSQL schema…
[Turn 26] GPT-OSS: Yes — and here’s the pseudo-code to implement it.

Use it in a responsible manner. Adding powerful models in this loops could have deep implications. Change models as needed in the scripts to fit your tech stack. Stay curious and feed the loops!
