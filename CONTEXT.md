# Smith Wiki research

Research wikis written by agents from captured evidence, one repository per
research.

## Research

**Research**:
One question and its answer as a wiki in its own repository: a card, notes,
entities, and source pages. It starts from zero and never reads another
research's working state.
_Avoid_: Thread, topic wiki, project

**Card**:
The research's `index.md`: the answer in one screen, shown as an image under
the post that raised the question.
_Avoid_: Summary, report

**Note**:
One claim, distinction, relationship, or implication per page, titled with a
declarative sentence and linked to the notes it belongs with.
_Avoid_: Article, concept page

**Published version**:
A tag `vN` on main, served at `smith.wiki/<research>/`. It is frozen; other
researches cite it as a source.
_Avoid_: Release, snapshot

**Registry**:
The `smith.wiki` home page listing every published research.

## Evidence

**Source**:
One URL whose content the research relies on, such as a paper, a documentation
page, or a repository README. Other files of a repository are read at a fixed
commit and cited by locator.
_Avoid_: Document, reference, work

**Source page**:
The page in `sources/` that annotates one Source. A Source may have no page.

**Capture**:
The stored copy of a Source at one moment: its Original and a Markdown version
of it. A Source has one current Capture per research; recapturing replaces it,
and the earlier one stays only in the archive.
_Avoid_: Download, snapshot, version

**Original**:
The exact bytes a URL returned when it was captured; never modified.

**Locator**:
The position of a claim inside a Source: a heading or exact text for a web
page, a page or section for a paper, a file and heading for a repository.

## Search

**Search scope**:
The part of a research a search covers: `sources` (its current Captures) or
`wiki` (its pages, Source pages included).

**Chunk**:
A piece of a Capture or a page that search stores and returns. Chunks change
whenever chunking changes; a Locator does not.
