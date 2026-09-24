# sw

Tools and site kit shared by every Smith Wiki research. A research is one
repository in the [smith-wiki](https://github.com/smith-wiki) organization and
one wiki at `https://smith.wiki/<research>/`; this module is everything the
repositories have in common, so they hold only their pages.

- `bin/sw`: the command line (`sw help`). `sw new` creates a research
  repository; `sw fetch`, `sw search`, `sw commit`, `sw pr`, `sw checks`, and
  `sw merge` work inside one; `sw publish` tags a version and deploys it.
- `GUIDE.md`: the contract agents follow inside a research (`sw guide`).
- `lib/`: capture and search (`fetch.py`, `search.py`, `search_eval.py`) and
  checks (`tidy.py`, `check_ascii.py`, `check_internal_links.py`).
- `site/`: the Eleventy kit. `sw build` stages a research's `index.md`,
  `sources/`, `notes/`, and `entities/` with the kit's layouts, section pages,
  and assets, and builds under the Pages base path.
- `.github/workflows/check.yml` and `deploy.yml`: reusable workflows each
  research calls.

A research starts as a conversation with an agent in the research folder,
`~/sm-th/research` (override with `SW_RESEARCH_ROOT`), whose `AGENTS.md` sends
the agent to `sw guide`. Once the question is clear, the agent chooses the
repository name, title, and English question and runs `sw new`, which creates
the research in a subfolder of that folder wherever it is run.

## Use

A research repository's `flake.nix` takes this flake as an input and exposes
its dev shell, which puts `sw` on the path:

```nix
{
  inputs.sw.url = "github:sm-th/sw";
  outputs = { sw, ... }: { inherit (sw) devShells; };
}
```

Captures live in a private Cloudflare R2 bucket under the SHA-256 of their
original; each research keeps its own URL pointers under
`research/<research>/urls/` and its own Qdrant collection behind the alias
`sw-<research>`, so researches never see each other's working state. The local
capture cache is `~/.cache/sw/`.

## Secrets

`sw fetch` and `sw search` read the secrets declared in `secretspec.toml` from
one dotenv file, `~/.config/sw/.env` (override with `SW_ENV_FILE`).

## Publishing

The organization site `smith-wiki/smith-wiki.github.io` owns the `smith.wiki`
domain, so GitHub Pages serves each public research repository at
`smith.wiki/<repository>/`. `sw new` creates a public research by default:
it enables Pages, tags the repository `smith-wiki-research`, and asks the
registry on `smith.wiki` to rebuild; every merge to main redeploys the site.
`sw new --private` keeps a research private until `sw make-public`.
`sw publish vN` freezes a version as a tag and redeploys with it.
