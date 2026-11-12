# Making a release

A release is a version number in `pyproject.toml`, a Git tag of the same
name on the commit that sets it, a GitHub release, and a container image
tagged with it. The **Release** workflow does the first three steps, and
starts the image build; you edit the release notes and publish.

Versions are `<major>.<minor>.<patch>`, without a `v`: `0.2.0`, not
`v0.2.0`. Bump the major version for changes an operator must act on, such
as a configuration option removed or renamed, or a change to what
networking-mlnx sees; the minor version for features; the patch version for
fixes only. Schema changes are fine in any of them: `--db-upgrade` applies
them (see [Upgrades](#after-publishing)).

## Before you start

- **`main` is green**, and everything to release is merged.
- **Choose the version.** It must be newer than both `pyproject.toml`'s
  version and the latest tag.
- **The workflow can push to `main`.** It commits the version bump to
  `main` as `github-actions[bot]`. If `main`'s ruleset requires pull
  requests, reviews or status checks, add GitHub Actions to its bypass
  list, or the push fails (see Branch protection).

## Run the workflow

In GitHub: **Actions** → **Release** → **Run workflow**, on `main`, with the
version, such as `0.2.0`. Or with the GitHub CLI:

```sh
gh workflow run release.yaml --repo stackhpc/alternative-fabric-manager -f version=0.2.0
gh run watch --repo stackhpc/alternative-fabric-manager
```

The workflow:

1. runs the nemesis on `main`, about 7 minutes: randomized HA faults
   against live requests, with the history checked afterwards (see the
   functional-tests skill), and goes no further if it fails;
2. refuses to run from any branch but `main`;
3. checks the version: well formed, not already a tag, and newer than both
   `pyproject.toml`'s version and the latest tag;
4. sets the version in `pyproject.toml` and `uv.lock` with `uv version`,
   using the uv in `.uv-version`, and fails if anything else changed;
5. commits **Release 0.2.0** to `main` and pushes it;
6. tags that commit `0.2.0`, an annotated tag, and pushes the tag;
7. opens a **draft** release for the tag, with GitHub's generated release
   notes;
8. starts **Build and publish image** on the tag, which builds and pushes
   `ghcr.io/stackhpc/alternative-fabric-manager:0.2.0` for amd64 and arm64.
   A tag pushed by a workflow doesn't start other workflows by itself, so
   the release workflow starts it.

## Check the image

The image build runs separately; find it under **Actions** → **Build and
publish image**, on the tag. Once it has finished, note the image's digest,
which deployments pin to:

```sh
docker buildx imagetools inspect ghcr.io/stackhpc/alternative-fabric-manager:0.2.0
```

Don't publish the release until the image exists.

## Adjust the release notes

The draft's notes list the pull requests merged since the previous tag, and
their authors. Edit them in **Releases** → the draft → **Edit**, or write
them to a file and:

```sh
gh release edit 0.2.0 --repo stackhpc/alternative-fabric-manager --notes-file notes.md
```

Keep the generated list, and add above it what an operator needs:

- **Highlights**: what's new, in a sentence or two each.
- **Upgrade notes**: anything to do when upgrading, such as
  - a schema change: run `alternative-fabric-manager --db-upgrade` before
    starting the new version, which Kayobe's `pre.yml` does
    ([kayobe.md](kayobe.md));
  - configuration options added, renamed or removed, and new defaults;
  - changes networking-mlnx or Nova need, such as timeouts;
  - for HA mode, whether instances of the old and new versions can run
    together during a rolling upgrade (see [specs/ha.md](../specs/ha.md),
    Upgrades).
- **The image**: `ghcr.io/stackhpc/alternative-fabric-manager:0.2.0`, and
  its digest.
- **Known limitations** that are new or changed.

## Publish

Check the notes and the tag, then in **Releases** → the draft → **Edit**,
leave **Set as a pre-release** cleared, keep **Set as the latest release**
ticked, and **Publish release**. Or:

```sh
gh release edit 0.2.0 --repo stackhpc/alternative-fabric-manager --draft=false --latest
```

## After publishing

- Deploy by the image's digest, not the tag alone.
- Upgrade the database schema before starting the new version:
  `alternative-fabric-manager --config-file … --db-upgrade`. An SQLite
  database upgrades itself.

## If the workflow fails

Each step's result stays, so continue from the step that failed rather than
starting again:

| Failed step | State | What to do |
| --- | --- | --- |
| Validation, or the bump | Nothing changed | Fix the cause, and run the workflow again |
| Pushing to `main` | Nothing pushed | Usually branch protection: allow GitHub Actions to bypass it, and run again |
| Pushing the tag | The bump is on `main`, untagged | Tag the **Release** commit yourself: `git tag -a 0.2.0 -m "Release 0.2.0" <commit> && git push origin 0.2.0` |
| Opening the release | The tag is pushed | `gh release create 0.2.0 --draft --verify-tag --title 0.2.0 --generate-notes` |
| Starting the image build | The release is drafted | **Actions** → **Build and publish image** → **Run workflow**, with the tag `0.2.0` as its ref |

Running the workflow again after the bump reached `main` fails its
validation, since the version is then no longer newer than
`pyproject.toml`'s: finish by hand as above. Never move or delete a tag
once its release is published: deployments and the image refer to it.

## Branch protection

`main`'s ruleset (**Settings** → **Rules** → **Rulesets**) should:

- **require a pull request** before merging, and **block force pushes**;
- **require these status checks**: `Test` (lint, formatting and unit
  tests) and `Functional (failover)`, which every pull request gets: it
  runs the failover suite when the pull request touches HA code, the
  schema, the API, the harness or the dependencies, and passes in seconds
  otherwise. A check only appears in the ruleset's search once it has run,
  so add it after a pull request has run it;
- leave **require branches to be up to date** off, unless a merge queue is
  in use, or every merge makes each open HA pull request run the 15-minute
  suite again;
- list **GitHub Actions** as a bypass actor, for the Release workflow's
  push of the version bump.

Don't require `Pytest`, which only annotates `Test`'s results, or the
nemesis, which runs in the release workflow and every night
(`nightly.yaml`), not on pull requests: a nightly failure opens an issue
naming its seed.
