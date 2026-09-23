# Study companion — spec

Date: 2026-09-23
Status: approved

## Goal

Turn the bot from "solver + manual MCQ bank" into a study companion that knows
the user's four books, their previous-year questions (PYQs), and what to study
next — with **no slash commands**. The bot works out what every message is and
acts on it.

Books in scope:

| Book | Subject |
|---|---|
| Indian Polity — M. Laxmikanth | Polity |
| Indian History — M. Abdul Kareem (Max Publications) | History |
| Quantitative Aptitude — R.S. Aggarwal | Arithmetic |
| General Science — Lucent's | Science |

Exams: TSLPRB SI and PC (user applied for both; rank for SI by default, show PC too).

Vector search (RAG) is part of the core: every page and PYQ the user sends is
embedded, and every answer retrieves from the user's own material first.

Out of scope for this spec: study planner and exam countdown.

## User experience

### Photos

**Every photo is stored and indexed, whatever it is.** The photo itself is kept
by its Telegram `file_id` (Telegram hosts the image; the bot can resend it later),
and its text goes into `pages` and the vector index. Nothing the user sends is
thrown away.

Each photo is read once by the vision model, which returns the page **kind**,
the book, subject, topic, page number and the extracted content in one JSON
response. Those fields are the index.

| Kind | Action | Reply (example) |
|---|---|---|
| `cover` | Set current book | "Karim — Indian History. Send the index pages next." |
| `index` | Upsert chapters (number, title, page range, topic list) | "Saved ch.5–6 of Karim: Pre-Mauryan Age, Mauryan Age." |
| `pyq` | Store each question with exam, year, number, options, answer, topic | "Saved 12 PYQs (SI 2019) — Mauryan Age." |
| `content` | Store page text, generate MCQs (existing `mcq` flow), mark topic as read | "Mauryan administration — 8 practice questions added." |
| `question` | Store it, then solve it | The answer |
| `other` | Store it, then solve / explain | The answer |

If the photo has a caption (e.g. "what should I study?"), the caption is
answered after saving, with what was just saved in context.

Every save reply carries inline buttons: `↩️ Undo` · `🔁 Wrong type` (single photos).
When the model's confidence is low it asks instead, with two buttons
("PYQ page" / "Notes page"). Buttons, never commands.

### Text

A text-model router classifies each message into one intent:

| Intent | Example | Action |
|---|---|---|
| `study_plan` | "what should I study", "which chapter first" | Ranked topic list (see Ranking) |
| `lookup` | "help with Q14", "explain question 7", "this one" | Fetch that PYQ from current focus, solve with retrieved context |
| `search` | "PYQs on Ashoka", "what did Karim say about rajukas" | Vector search over pages + PYQs, answer from them |
| `quiz` | "quiz me", "test me on polity" | Start quiz (existing `quiz` flow) |
| `stats` | "how am I doing", "weak topics" | Existing stats / weak rendering |
| `solve` | anything else | Solver, with history + focus + retrieved context |

### Focus

`chat_data["focus"]` holds the last book / chapter / page / PYQ batch the user
sent, for 60 minutes. "Q14" resolves against the focus batch first, then the
focus chapter, then the most recent PYQs. When nothing matches, the bot says so
and lists what it has ("I have Q1–12 from SI 2019, Mauryan Age").

## Retrieval (vector DB)

Embedding model: `Qwen/Qwen3-Embedding-8B` via Nebius `/embeddings`, requested
at `dimensions=1024` (verified working on the account's key).

What gets embedded, at save time:

| Item | Text embedded |
|---|---|
| Page | Page text (split into ~800-char chunks with overlap) |
| PYQ | Question + options + answer |
| Chapter | Book + chapter title + topic list |

What retrieval is used for:

1. **Grounded answers.** Every `solve`, `lookup` and `search` retrieves the top
   ~6 chunks and PYQs (filtered by the user, boosted toward the focus chapter)
   and puts them in the prompt. Answers cite what they used ("Karim p.152",
   "SI 2019 Q14").
2. **Topic mapping.** A new PYQ is matched to its nearest chapter embedding, so
   PYQs get a book topic even when their page doesn't name one.
3. **Similar PYQs.** After solving a question, the bot appends "Asked before:"
   with the 2–3 nearest PYQs, when they are close enough.
4. **Dedupe.** A PYQ whose vector is ≥ 0.97 cosine-similar to a stored one is
   treated as a duplicate (the same question printed in two books).

Exact lookups ("Q14", "chapter 6") stay plain SQL. Vectors are for meaning,
not numbers.

Storage:
- **Postgres (production):** `pgvector` extension, `embedding vector(1024)`
  columns, HNSW index with cosine distance. Neon and Supabase both ship it.
- **SQLite (local dev):** embedding stored as a float32 blob; brute-force cosine
  in Python. Fine for a few thousand rows.

## Ranking ("what should I study")

Per syllabus topic:

```
weight   = PYQ count for topic / total PYQs   (SI and PC counted separately; SI shown first)
coverage = 0 not started · 0.5 pages read · 1 pages read and quiz accuracy ≥ 70 %
weakness = 1 - quiz accuracy on the topic (0 when never quizzed)
score    = weight × (1 - 0.7·coverage) × (1 + weakness)
```

Until at least 30 PYQs exist the bot says the weightage is thin and falls back
to a seeded estimate table (`syllabus.py`), every number marked "estimate".

The reply also lists **gaps**: syllabus subjects with no book added
(Reasoning, Geography, Economy, Telangana, Current Affairs), each with its
PYQ share when known.

## Data model

New tables, in both backends (`db.py` SQLite and `db_pg.py` Postgres), all keyed by `user_id`.

```
books     id, user_id, title, subject, created_at
chapters  id, book_id, number, title, page_start, page_end, topics (json text), UNIQUE(book_id, number)
pyqs      id, user_id, book_id, chapter_id NULL, exam ('SI'|'PC'|''), year NULL,
          number NULL, question, options (json text), answer NULL, subject, topic,
          created_at, UNIQUE(user_id, question)
pages     id, user_id, book_id NULL, chapter_id NULL, page_no NULL, kind, subject, topic,
          text, file_id, file_unique_id, created_at, UNIQUE(user_id, file_unique_id)
```

plus vector storage:

```
chunks    id, user_id, source ('page'|'pyq'|'chapter'), source_id, text,
          embedding vector(1024), subject, topic
```

`pages` keeps the raw text of every photo read, so re-embedding with a new
model never needs new photos. The existing `questions` / `attempts` / `review` tables stay as they are;
generated MCQs keep feeding them.

Undo deletes the rows created by that message (tracked by a `batch_id` stored on
each row and in the reply's callback data).

## Code layout

| File | Change |
|---|---|
| `router.py` (new) | `read_page(image) -> PageRead` (vision JSON) and `classify_text(text, focus) -> Intent` (text JSON) |
| `library.py` (new) | Save/undo for books, chapters, PYQs, pages; focus helpers; Q-number lookup |
| `retrieval.py` (new) | Chunking, embedding calls, store/search for both backends |
| `ranking.py` (new) | Score topics, render the study plan |
| `syllabus.py` (new) | Subject list, seeded estimate weightage, book → subject map |
| `db.py` / `db_pg.py` | New tables and functions |
| `ai_client.py` | `embed(texts) -> list[list[float]]` |
| `main.py` | `handle_photo` / `handle_text` call the router; add mode and all slash commands except `/start` removed |
| `setup_botfather.py` | Clear the command menu |
| `prompts.py` / `exam_prompts.py` | Page-reading and intent prompts |
| `tests/` (new) | Router JSON parsing, ranking maths, chunking, SQLite vector search, DB round-trips |

JSON parsing reuses the tolerant parser in `mcq.py` (strip fences, find object).

## Cost and latency

- One vision call per page (classify + extract together).
- One embedding call per saved page / PYQ batch (batched inputs).
- One small text call per text message for routing, one embedding call for the
  query, plus the answer call.
- Content pages add the existing MCQ generation call.

## Risks

- **Misread question numbers / years.** Save replies show what was read; undo is one tap.
- **Wrong page kind.** Low-confidence reads ask instead of guessing.
- **Wrong book.** Model picks from the four known books; unknown → asks.
- **Irrelevant retrieval.** Only chunks above a similarity threshold go into
  the prompt; below it the bot answers without them rather than with noise.
- **Thin PYQ data early on.** Ranking says so and uses marked estimates.

## Decisions

1. Practice MCQs are generated automatically from every content page.
2. Slash commands are removed (only `/start`, which Telegram requires, stays).
   Welcome text explains "just send photos or ask".
