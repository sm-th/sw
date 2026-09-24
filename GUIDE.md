# Smith Wiki research contract

A research is one repository and one wiki: a question, the sources that answer
it, and short linked notes that carry the depth. Everything written to the
repository or to GitHub is English, whatever language the conversation uses.

## What a research is

- **It starts from zero.** Work only from this repository's pages, the
  sources it captures, and published versions of other researches cited as
  sources. Never read or edit another research's working state.
- **It ends as a frozen version.** `sw publish v1` tags main and serves it at
  `https://smith.wiki/<research>/`. A later change is a new version, never an
  edit of the published one. Publish only when the user asks.
- **Other researches are sources.** Cite a published research by capturing the
  page you rely on with `sw fetch` and linking its source page, like any other
  source.

## Doing the research

1. **Orient.** Run `sw`, read `index.md` and the question Issue, and run
   `sw search` with the question. A hit is a pointer: read the lines it names
   before relying on it.
2. **Start from the author's words.** When the question comes from a post or a
   message, its comparisons, doubts, and plans are the questions to dig into,
   not the page it links to. Say what the author claimed and whether the
   evidence holds it.
3. **Read deeply.** Capture every source you rely on with
   `sw fetch URL SOURCE`, where SOURCE is the slug of its page in `sources/`,
   then read the Markdown copy the command prints instead of fetching the page
   again. Follow links out of what you read (cited papers, documentation,
   source code, prior work, alternatives, critiques) for as long as each new
   source adds a claim or a connection; stop when it no longer does. Record in
   the pull request which links you followed and which you skipped, and why.
   A repository source is its README fetched by raw URL at a fixed commit;
   other files are read at that commit and cited by locator.
4. **Write notes.** Depth lives in `notes/`, one claim per page, like a slip in
   a Zettelkasten. Before writing one, search the research's pages
   (`sw search QUERY --in wiki`) and extend the note that already makes the
   claim instead of adding a near-duplicate. Prefer fewer, sharper notes; a
   research rarely needs more than twenty.
5. **Write the card.** `index.md` answers the question for a reader who sees
   nothing else: it is shown as an image under the author's post. Keep it to
   one screen.
6. **Deliver.** `sw commit`, then `sw pr TITLE BODY`, where the body is the
   answer: findings, pages added or changed, links followed and skipped, and
   open questions. Wait for `sw checks`, then `sw merge`.

## Pages

- `index.md`, the card. Front matter has exactly `title` (the answer stated as
  a claim), `question`, `brief` (the direct answer in at most 280 characters,
  posted as the reply text), and `date` (YYYY-MM-DD). The body starts with
  `## Answer` and stays under 200 words: the answer first, then links to the
  notes that carry it.
- `notes/`: one claim, distinction, relationship, or implication per page,
  titled with a declarative sentence, 50 to 250 words, with `title` and
  `summary` front matter. Every note cites the sources behind it and links to
  at least one other note or entity, saying why the two belong together; state
  the connection on both pages.
- `entities/`: named people, organizations, projects, products, and places,
  answering "what is this?", with `title` and `summary` front matter.
- `sources/`: one annotated page per captured URL. Front matter holds `title`,
  `summary`, `url`, `author` (a person, several people, or the responsible
  organization), optional `publisher`, `published` (YYYY-MM-DD, the date the
  source itself states; omitted when it states none), `kind` (`article`,
  `book`, `documentation`, `paper`, `repository`, `specification`, `video`,
  or `webpage`), and `retrieved` and `sha256`, which `sw fetch` writes. The
  body has `## Overview` (80 to 150 words: what the source is, who wrote it for
  whom and why, how far to trust it) and `## Key points` (at most seven claims
  the research relies on, in your own words, each with a locator in
  parentheses: a heading or exact text for a web page, a page or section for a
  paper, a file and heading for a repository).

There are no concept pages. Never coin a term: name a thing only with words a
source uses, or with the author's own words when the author coined it. A
distinction worth naming is a note whose title states it.

Section indexes are generated. Page paths are permanent: when merging or
renaming a page, update every inbound link in the same change.

## Writing

Write in your own words and quote only when the wording matters. Cite with
readable author-date links to source pages, using the source's `author` and
the year of `published`, or `n.d.` when it has none, such as
`([Lamport, 2025](../../sources/leslie-lamport-tla-homepage/))`. Attribute
strong claims to their source. Prefix interpretation with **Inference:**, open
gaps with **Uncertainty:**, and conflicting evidence with **Contradiction:**.

## Repository work

- One coherent outcome is one Issue, one `issue/<number>-<slug>` branch, and
  one pull request that closes the Issue. `sw new` opens the question as the
  first one; `sw task SLUG TITLE REQUEST` opens later ones.
- Commit with `sw commit` using Conventional Commits; it tidies the pages it
  commits and rejects broken links between them.
- GitHub Actions is the verification environment: the check builds the site
  under its published path and rejects broken links. Watch it with
  `sw checks`.
